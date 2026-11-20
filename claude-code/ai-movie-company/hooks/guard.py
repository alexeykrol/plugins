#!/usr/bin/env python3
"""ai-movie-company hook: the install guard, the terms gate and the session mode record.

Wired in hooks/hooks.json (plugin hooks are active in every session where the plugin is enabled):
  PreToolUse  Bash|Write|Edit|MultiEdit|NotebookEdit -> guard.py pretool
  UserPromptSubmit                                   -> guard.py prompt

pretool — one decision per tool call, printed as PreToolUse JSON (or nothing = no opinion):
  1. STOP (deny) a Bash command that brings outside code onto the machine: package installs
     (npm/pnpm/yarn/bun install|add|ci|exec, npx/pnpx/bunx, pip/pipx/uv/uvx, brew/port, cargo,
     gem, go install|get, conda/mamba, apt/yum/dnf, composer, poetry, deno install), `claude plugin
     install` / `claude plugin marketplace add` / `claude mcp add`, a download piped into a shell or
     an interpreter, a download followed by running it, a macOS `installer -pkg`. The reason tells
     Claude to vet it with the plugin's `vet` skill and to explain the stop to the person.
     Let through without a stop: the plugin's own engine (`.../scripts/vp …`, incl. `vp extras
     install`), the plugin's own installer from the official store URL piped into bash/sh, and
     `npm ci --ignore-scripts` in the Remotion workspace (<vp_home>/remotion/…) whose
     package-lock.json is byte-equal to the plugin's own.
  2. A stopped command passes ONCE after the person approved it: `vp guard approve <id>` (needs
     the person's own answer after the stop — vp.gates.human) writes a single-use approval tied to
     the sha256 of the exact command, valid 30 minutes; this hook consumes it.
  3. TERMS: installs and publishing (the installer, `vp extras install`, an approved install,
     `vp approve set`, `vp run`/`vp stage` of a job that uploads) wait for the accepted terms
     (<vp_home>/consent.json, version TERMS_VERSION; `vp consent …`).
  4. PROTECTED state: Claude's tools may not write the guard's own files (consent, approvals,
     guard settings, mode, the user-input record) — only the plugin's engine does, behind the
     human gate. Reading them with cat/ls/grep is fine.
  5. FEWER QUESTIONS (opt-in, `vp mode enable`): in Elsa's workspace a single plain call of the
     plugin's own engine is allowed without Claude's permission card (permissionDecision allow).
prompt — remembers the session's permission mode (hook input `permission_mode`) and cwd in
  <runtime>/session_modes.json, so `vp mode status` can say whether this session asks before
  every command. Never stores what the person wrote.

Off switch (documented, explicit): env AIMC_GUARD=off for the process that runs Claude, or
`vp guard off` (behind the person's own answer) which writes <vp_home>/guard/settings.json.
The terms gate and the protected state stay on even then.

FAILS CLOSED on what matters, open on the rest (owner decision 2026-11-19). If the decision above
crashes or runs past INTERNAL_DEADLINE_S (a SIGALRM watchdog, well inside the 10 s hook timeout
of hooks.json), `fail_safe` decides from the raw event with a few plain, linear regexes:
  - a Bash command that LOOKS like it adds outside code (install_shaped: package-manager install
    verbs, npx/uvx/…, `claude plugin install|marketplace add`, `claude mcp add`, a download piped
    into a shell or interpreter, `installer -pkg`) -> deny, with a plain reason and the way out;
  - a Bash command or a Write/Edit that names the guard's protected files -> deny;
  - anything else (ls, git, ffprobe, the plugin's own engine, editing a script …) -> no opinion,
    so a guard bug never blocks normal work.
  The guard's off switch is honoured on this path too: with AIMC_GUARD=off (or `vp guard off`)
  install-shaped commands go through, so a broken hook can never brick the session — the way out
  is to start Claude with AIMC_GUARD=off (Elsa says so in the stop reason and in her contract).
  If fail_safe itself fails, the hook prints nothing (no opinion).
Not covered (stated in docs/safety-terms-mode-2026-11-19.md): python3 missing or a syntax error
in this file, and Claude Code's own hook timeout — both run before or outside this code and are
fail-open by Claude Code's rules for non-blocking hook errors.

Contract: standard library only, fast, exit 0 on every path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMS_VERSION = "2026-11-21-draft"      # keep in sync with vp.consent.TERMS_VERSION (a test checks)
INSTALLER_URL = ("https://raw.githubusercontent.com/alexeykrol/plugins/main/claude-code/"
                 "ai-movie-company/install.sh")
APPROVAL_TTL_S = 30 * 60
MAX_PENDING = 20
MAX_SESSIONS = 50
INTERNAL_DEADLINE_S = 4.0                 # < the 10 s hook timeout in hooks.json
OWN_ROOT = Path(__file__).resolve().parent.parent

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
INTERPRETERS = SHELLS | {"python", "python2", "python3", "node", "perl", "ruby", "php", "osascript",
                         "deno", "bun", "pwsh", "source", "."}
DOWNLOADERS = {"curl", "wget", "fetch", "http", "https", "aria2c"}
WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "nohup", "time", "nice", "exec", "noglob",
            "stdbuf", "caffeinate"}
READ_ONLY = {"cat", "ls", "head", "tail", "grep", "rg", "wc", "stat", "file", "jq", "less", "more"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", "\n"}
# env assignments a pre-approved engine call may carry (VP_PYTHON would run another interpreter)
PREAPPROVE_ENV = {"VP_JUDGE", "CLAUDE_PLUGIN_DATA"}
INSTALLER_CURL_FLAGS = {"-f", "-s", "-S", "-L", "-fsSL", "-fsS", "-sSL", "-fSL", "-sL", "-fL",
                        "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
                        "--tlsv1.2", "--proto=https"}

PKG_VERBS = {
    "npm": {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
            "isntall", "add", "ci", "clean-install", "ic", "install-clean", "isntall-clean",
            "install-ci-test", "cit", "install-test", "it", "update", "up", "upgrade", "udpate",
            "exec", "x", "link", "ln", "rebuild", "rb"},
    "pnpm": {"install", "i", "add", "update", "up", "upgrade", "dlx", "exec", "link", "ln",
             "rebuild", "rb", "import"},
    "yarn": {"install", "add", "dlx", "up", "upgrade", "upgrade-interactive", "link", "global",
             "create"},
    "bun": {"install", "i", "add", "a", "update", "x", "link", "create"},
    "pip": {"install", "download", "wheel"},
    "pipx": {"install", "run", "inject", "upgrade", "upgrade-all", "reinstall", "reinstall-all"},
    "uv": {"add", "sync", "tool", "pip", "run"},
    "brew": {"install", "reinstall", "upgrade", "tap", "bundle", "cask"},
    "port": {"install", "upgrade"},
    "cargo": {"install", "add", "binstall"},
    "gem": {"install", "update"},
    "go": {"install", "get", "run"},
    "conda": {"install", "create", "update", "upgrade"},
    "mamba": {"install", "create", "update", "upgrade"},
    "micromamba": {"install", "create", "update"},
    "apt": {"install", "upgrade", "full-upgrade"},
    "apt-get": {"install", "upgrade", "dist-upgrade"},
    "yum": {"install", "update", "upgrade", "localinstall"},
    "dnf": {"install", "update", "upgrade"},
    "composer": {"require", "install", "update", "global", "create-project"},
    "poetry": {"add", "install", "update", "self"},
    "deno": {"install", "add"},
    "snap": {"install"},
    "flatpak": {"install"},
    "mas": {"install"},
}
ALWAYS_RUNS_OUTSIDE_CODE = {"npx", "pnpx", "bunx", "uvx", "pipx-run"}
PKG_DESCR = {
    "npm": "npm packages", "pnpm": "npm packages (pnpm)", "yarn": "npm packages (yarn)",
    "bun": "npm packages (bun)", "pip": "Python packages (pip)", "pipx": "a Python program (pipx)",
    "uv": "Python packages (uv)", "brew": "Homebrew software", "port": "MacPorts software",
    "cargo": "Rust crates", "gem": "Ruby gems", "go": "Go modules", "conda": "conda packages",
    "mamba": "conda packages", "micromamba": "conda packages", "apt": "system packages",
    "apt-get": "system packages", "yum": "system packages", "dnf": "system packages",
    "composer": "PHP packages", "poetry": "Python packages (poetry)", "deno": "Deno modules",
    "snap": "a snap package", "flatpak": "a flatpak app", "mas": "a Mac App Store app",
}


# ---- places ------------------------------------------------------------------------------------

def vp_home() -> Path:
    v = (os.environ.get("VP_HOME") or "").strip()
    return Path(v).expanduser() if v else Path.home() / ".ai_video_pipeline"


def runtime_dir() -> Path:
    for key in ("VP_RUNTIME_DIR", "CLAUDE_PLUGIN_DATA"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return Path(v).expanduser()
    return Path.home() / ".ai_video_pipeline" / "runtime"


def guard_dir() -> Path:
    return vp_home() / "guard"


def protected_paths() -> list[Path]:
    h = vp_home()
    return [h / "consent.json", h / "mode.json", guard_dir() / "settings.json",
            guard_dir() / "approvals.json", guard_dir() / "pending.json",
            runtime_dir() / "user_input.json", runtime_dir() / "session_modes.json"]


PROTECTED_MARKERS = ("consent.json", ".ai_video_pipeline/guard/", ".ai_video_pipeline/mode.json",
                     "user_input.json", "session_modes.json", "approvals.json", "AIMC_GUARD")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="microseconds")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def guard_on() -> bool:
    if (os.environ.get("AIMC_GUARD") or "").strip().lower() == "off":
        return False
    return read_json(guard_dir() / "settings.json").get("enabled", True) is not False


def terms_accepted() -> bool:
    c = read_json(vp_home() / "consent.json")
    return c.get("version") == TERMS_VERSION and bool(c.get("accepted_at"))


def command_sha(command: str) -> str:
    return hashlib.sha256(command.strip().encode("utf-8")).hexdigest()


# ---- parsing -----------------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)",
                      re.S)


def strip_heredocs(command: str) -> str:
    """Heredoc bodies are data (a file being written), not commands."""
    return _HEREDOC.sub(lambda m: "<<HEREDOC", command)


def tokenize(command: str) -> list[str]:
    text = strip_heredocs(command).replace("\\\n", " ").replace("\n", " ; ")
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:                     # unbalanced quotes: fall back to a plain split
        return re.findall(r"[;&|()<>]+|[^\s;&|()<>]+", text)


def segments(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """[(operator before the segment, words)]; redirection targets are dropped."""
    out: list[tuple[str, list[str]]] = []
    cur: list[str] = []
    op = ""
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t in SEPARATORS or (set(t) <= set(";&|()") and t):
            if cur:
                out.append((op, cur))
            cur, op = [], t
            continue
        if set(t) <= set("<>&") and t:     # >, >>, <, >&, <<, &> : drop the target too
            skip_next = True
            continue
        cur.append(t)
    if cur:
        out.append((op, cur))
    return out


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_env(words: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(words) and _ASSIGN.match(words[i]):
        k, _, v = words[i].partition("=")
        env[k] = v
        i += 1
    return env, words[i:]


def unwrap(words: list[str]) -> list[str]:
    """Drop wrappers (sudo, env, timeout 10, nice -n 5, …) and env assignments."""
    for _ in range(8):
        _, words = split_env(words)
        if not words:
            return words
        prog = os.path.basename(words[0])
        if prog in WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in ("-u", "-g", "-n", "-C", "-o", "-e", "-i") and words:
                    words.pop(0)
            continue
        if prog == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                words.pop(0)
            words = words[1:]                                   # the duration
            continue
        return words
    return words


def positional(args: list[str], valued: tuple[str, ...] = ()) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in valued:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


# ---- the plugin's own commands -----------------------------------------------------------------

def plugin_roots() -> list[Path]:
    roots = [OWN_ROOT]
    env = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if env:
        roots.append(Path(env))
    return roots


_PLUGIN_VP = re.compile(r"/\.claude/plugins/(?:cache|marketplaces)/[^/]+/ai-movie-company/"
                        r"(?:[^/]+/)?scripts/vp$")


def is_own_vp(path: str, cwd: str = "") -> bool:
    if not path:
        return False
    if path.replace("\\", "/").endswith("${CLAUDE_PLUGIN_ROOT}/scripts/vp") \
            or path in ("${CLAUDE_PLUGIN_ROOT}/scripts/vp", "$CLAUDE_PLUGIN_ROOT/scripts/vp"):
        return True
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        rp = p.resolve()
    except OSError:
        return False
    for root in plugin_roots():
        try:
            if rp == (root / "scripts" / "vp").resolve():
                return True
        except OSError:
            continue
    return bool(_PLUGIN_VP.search(str(rp)))


def vp_args(words: list[str]) -> list[str]:
    """Arguments of an engine call with `--home X` removed."""
    out: list[str] = []
    i = 1
    while i < len(words):
        if words[i] == "--home":
            i += 2
            continue
        if not words[i].startswith("--home="):
            out.append(words[i])
        i += 1
    return out


_FAKE = re.compile(r"^publisher:\s*['\"]?fake['\"]?\s*(?:#.*)?$", re.M)


def is_rehearsal(job: str, cwd: str) -> bool:
    """request.yaml or the home config says `publisher: fake` — nothing leaves the machine."""
    for path in (Path(cwd or ".") / os.path.expanduser(job) / "request.yaml",
                 vp_home() / "config.yaml"):
        try:
            if _FAKE.search(path.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def vp_needs_terms(args: list[str], cwd: str) -> str | None:
    """What this engine call does that waits for the terms, else None."""
    if not args:
        return None
    cmd = args[0]
    if cmd == "extras" and len(args) > 1 and args[1] == "install":
        return "install the design add-ons"
    if cmd == "approve" and len(args) > 1 and args[1] == "set":
        return "publish a video"
    if cmd in ("run", "stage") and len(args) > 1 and not is_rehearsal(args[1], cwd):
        return "upload videos to YouTube"
    return None


# ---- classification ----------------------------------------------------------------------------

class Finding:
    def __init__(self, kind: str, what: str):
        self.kind, self.what = kind, what

    def __repr__(self) -> str:
        return f"Finding({self.kind!r}, {self.what!r})"


def _pkg_finding(prog: str, args: list[str]) -> Finding | None:
    verbs = PKG_VERBS.get(prog)
    if verbs is None:
        return None
    valued = ("--prefix", "-C", "--dir", "--cwd", "-w", "--workspace", "--registry", "--cache",
              "--userconfig", "--index-url", "-i", "--extra-index-url", "--python", "-p")
    pos = positional(args, valued)
    if prog == "yarn" and not pos:
        return Finding("package", "npm packages (yarn install)")
    if not pos:
        return None
    verb = pos[0]
    if prog == "uv":
        if verb == "pip" and len(pos) > 1 and pos[1] in ("install", "sync"):
            pass
        elif verb == "tool" and len(pos) > 1 and pos[1] in ("install", "run", "upgrade"):
            pass
        elif verb == "run" and any(a.startswith("--with") for a in args):
            pass
        elif verb not in ("add", "sync"):
            return None
    elif prog == "brew" and verb == "cask":
        pass
    elif verb not in verbs:
        return None
    if prog == "uv" and verb in ("pip", "tool") and len(pos) > 1:
        verb, pos = f"{verb} {pos[1]}", pos[1:]
    names = [p for p in pos[1:] if not p.startswith(("http", "./", "/"))] or pos[1:]
    what = PKG_DESCR.get(prog, prog)
    if names:
        what += ": " + " ".join(names[:6]) + (" …" if len(names) > 6 else "")
    return Finding("package", f"{what} ({prog} {verb})")


def classify_words(words: list[str], cwd: str, depth: int = 0) -> Finding | None:
    words = unwrap(words)
    if not words or depth > 3:
        return None
    prog = os.path.basename(words[0])
    args = words[1:]
    if is_own_vp(words[0], cwd):
        return None
    if prog in SHELLS or prog == "eval":
        if prog == "eval":
            return classify(" ".join(args), cwd, depth + 1)
        for i, a in enumerate(args):
            if a in ("-c", "-lc", "-ic", "-cl") and i + 1 < len(args):
                return classify(args[i + 1], cwd, depth + 1)
        return None
    if prog == "xargs":
        rest = positional(args, ("-I", "-n", "-P", "-L", "-d", "-E", "-s"))
        idx = next((i for i, a in enumerate(args) if a == (rest[0] if rest else None)), None)
        return classify_words(args[idx:], cwd, depth + 1) if idx is not None else None
    if re.fullmatch(r"python(?:\d(?:\.\d+)?)?", prog):
        if "-m" in args:
            i = args.index("-m")
            mod = args[i + 1] if i + 1 < len(args) else ""
            if mod in ("pip", "pipx", "uv", "poetry"):
                return _pkg_finding(mod, args[i + 2:])
        return None
    if re.fullmatch(r"pip(?:\d(?:\.\d+)?)?", prog):
        return _pkg_finding("pip", args)
    if prog in ALWAYS_RUNS_OUTSIDE_CODE:
        if "--no-install" in args or "--no" in args or "--offline" in args:
            return None
        pos = positional(args, ("-p", "--package", "--from", "--with"))
        return Finding("run-package", f"a program fetched from the internet and run at once "
                                      f"({prog} {' '.join(pos[:3])})".rstrip())
    if prog == "claude":
        pos = positional(args, ("--scope", "-s", "--transport", "-t", "-e", "--env", "-H",
                                "--header"))
        if pos[:2] in (["plugin", "install"], ["plugin", "i"], ["plugins", "install"]):
            return Finding("claude-plugin", "a Claude plugin: " + " ".join(pos[2:4]))
        if pos[:3] in (["plugin", "marketplace", "add"], ["plugins", "marketplace", "add"]):
            return Finding("claude-plugin", "a new Claude plugin store: " + " ".join(pos[3:4]))
        if pos[:2] in (["mcp", "add"], ["mcp", "add-json"], ["mcp", "add-from-claude-desktop"]):
            return Finding("claude-mcp", "a new MCP server for Claude: " + " ".join(pos[2:3]))
        return None
    if prog == "installer" and ("-pkg" in args or "-package" in args):
        return Finding("pkg", "a macOS installer package")
    if prog == "npm" and _npm_ci_allowed(args, cwd):
        return None
    return _pkg_finding(prog, args)


def _npm_ci_allowed(args: list[str], cwd: str) -> bool:
    """`npm ci --ignore-scripts` in the plugin's Remotion workspace with the plugin's own lock."""
    pos = positional(args, ("--prefix", "-C", "--registry", "--cache"))
    if not pos or pos[0] not in ("ci", "clean-install") or "--ignore-scripts" not in args:
        return False
    target = cwd
    if "--prefix" in args:
        i = args.index("--prefix")
        target = args[i + 1] if i + 1 < len(args) else target
    try:
        tdir = Path(os.path.expanduser(target or ".")).resolve()
        base = (vp_home() / "remotion").resolve()
    except OSError:
        return False
    if tdir != base and base not in tdir.parents:
        return False
    lock = tdir / "package-lock.json"
    for root in plugin_roots():
        own = root / "remotion" / "package-lock.json"
        try:
            if own.is_file() and lock.read_bytes() == own.read_bytes():
                return True
        except OSError:
            continue
    return False


def _is_installer_pipe(down: list[str], shell: list[str]) -> bool:
    d = unwrap(down)
    if not d or os.path.basename(d[0]) != "curl":
        return False
    if shell not in (["bash"], ["sh"]):      # no env, no flags, no wrapper on the shell side
        return False
    urls = [a for a in d[1:] if a.startswith("http")]
    flags = [a for a in d[1:] if not a.startswith("http")]
    return urls == [INSTALLER_URL] and all(f in INSTALLER_CURL_FLAGS for f in flags)


def classify(command: str, cwd: str = "", depth: int = 0) -> Finding | None:
    """The first reason to stop this command, else None."""
    if depth > 3 or not command or not command.strip():
        return None
    segs = segments(tokenize(command))
    fetched: set[str] = set()              # names of files a downloader wrote in this command
    for i, (op, words) in enumerate(segs):
        w = unwrap(words)
        if not w:
            continue
        prog = os.path.basename(w[0])
        # a download piped into a shell or an interpreter
        if op in ("|", "|&") and i > 0 and prog in INTERPRETERS:
            prev = unwrap(segs[i - 1][1])
            if prev and os.path.basename(prev[0]) in DOWNLOADERS:
                if _is_installer_pipe(segs[i - 1][1], words):
                    continue
                url = next((a for a in prev[1:] if "://" in a), "a web address")
                return Finding("pipe-to-shell", f"a script downloaded from {url} and run at once")
        if prog in DOWNLOADERS:
            fetched |= downloaded_names(w[1:])
        elif fetched and _runs_one_of(w, fetched):
            return Finding("download-run", "a file downloaded from the internet and then run")
        f = classify_words(words, cwd, depth)
        if f:
            return f
    raw = strip_heredocs(command)
    if re.search(r"<\(\s*(?:curl|wget)\b", raw) or (
            re.search(r"(?:\$\(|`)\s*(?:curl|wget)\b", raw)
            and re.search(r"(?:^|[\s;&|(])(?:ba|z|da|k)?sh\b|\beval\b|\bsource\b|\bpython3?\b|"
                          r"\bnode\b|\bperl\b|\bruby\b", raw)):
        return Finding("pipe-to-shell", "a script downloaded from the internet and run at once")
    return None


def downloaded_names(args: list[str]) -> set[str]:
    names: set[str] = set()
    for i, a in enumerate(args):
        if a in ("-o", "--output", "-O", "--output-document") and i + 1 < len(args) \
                and "://" not in args[i + 1]:
            names.add(os.path.basename(args[i + 1]))
        elif a.startswith("--output=") or a.startswith("--output-document="):
            names.add(os.path.basename(a.split("=", 1)[1]))
        elif "://" in a:
            names.add(os.path.basename(a.split("?", 1)[0].rstrip("/")))
    return {n for n in names if n}


def _runs_one_of(w: list[str], names: set[str]) -> bool:
    prog = os.path.basename(w[0])
    if prog in names and (w[0].startswith("./") or "/" in w[0]):
        return True
    if prog in ("chmod", "open", "installer"):
        return any(os.path.basename(a) in names for a in w[1:] if not a.startswith("-"))
    if prog in INTERPRETERS:                 # the script it runs is the first positional
        pos = positional(w[1:], ("-c", "-m", "-e", "-W", "-X"))
        return bool(pos) and "-c" not in w and "-m" not in w and os.path.basename(pos[0]) in names
    return False


def own_vp_calls(command: str, cwd: str) -> list[list[str]]:
    """Arguments of every plugin-engine call in the command."""
    out = []
    for _, words in segments(tokenize(command)):
        w = unwrap(words)
        if w and is_own_vp(w[0], cwd):
            out.append(vp_args(w))
    return out


def is_installer(command: str) -> bool:
    segs = segments(tokenize(command))
    return any(op == "|" and i > 0 and _is_installer_pipe(segs[i - 1][1], words)
               for i, (op, words) in enumerate(segs))


def touches_protected(command: str, cwd: str) -> bool:
    if not any(m in command for m in PROTECTED_MARKERS):
        return False
    if any(t in (">", ">>", ">|", "&>", "&>>") for t in tokenize(command)):
        return True
    for _, words in segments(tokenize(command)):
        w = unwrap(words)
        if not w:
            continue
        if is_own_vp(w[0], cwd) or os.path.basename(w[0]) in READ_ONLY:
            continue
        return True
    return False


def single_own_call(command: str, cwd: str) -> bool:
    """One plain call of the plugin's engine: no chaining, no substitution, safe env only."""
    if re.search(r"\$\(|`|<\(", command):
        return False
    toks = tokenize(command)
    segs = segments(toks)
    if len(segs) != 1 or segs[0][0]:
        return False
    for i, t in enumerate(toks):
        if set(t) <= set(";&|()") and t:
            return False
        if set(t) <= set("<>&") and t:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if not (nxt == "/dev/null" or (t == ">&" and nxt in ("1", "2"))):
                return False
    env, rest = split_env(segs[0][1])
    if any(k not in PREAPPROVE_ENV for k in env):
        return False
    return bool(rest) and is_own_vp(rest[0], cwd)


# ---- pending stops and approvals ---------------------------------------------------------------

def stop_id(command: str) -> str:
    return command_sha(command)[:12]


def record_stop(command: str, finding: Finding, cwd: str) -> str:
    sid = stop_id(command)
    path = guard_dir() / "pending.json"
    data = read_json(path)
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    items[sid] = {"command": command, "sha256": command_sha(command), "kind": finding.kind,
                  "what": finding.what, "at": iso(now()), "cwd": cwd}
    if len(items) > MAX_PENDING:
        items = dict(sorted(items.items(), key=lambda kv: str(kv[1].get("at")))[-MAX_PENDING:])
    write_json(path, {"items": items})
    with open(guard_dir() / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": iso(now()), "event": "stop", "id": sid, "kind": finding.kind,
                            "what": finding.what}, ensure_ascii=False) + "\n")
    return sid


def take_approval(command: str) -> bool:
    """Consume a valid single-use approval of exactly this command."""
    path = guard_dir() / "approvals.json"
    data = read_json(path)
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    sha = command_sha(command)
    a = items.get(sha)
    if not isinstance(a, dict):
        return False
    try:
        expires = datetime.fromisoformat(str(a.get("expires_at")))
    except ValueError:
        expires = None
    del items[sha]
    write_json(path, {"items": items})
    ok = expires is not None and now() <= expires
    with open(guard_dir() / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": iso(now()), "event": "approved-run" if ok else "approval-expired",
                            "id": a.get("id")}, ensure_ascii=False) + "\n")
    return ok


# ---- decisions ---------------------------------------------------------------------------------

def decision(kind: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def stop_reason(sid: str, finding: Finding) -> str:
    return (
        f"AI Movie Company install guard: STOPPED (id {sid}). This command would bring outside "
        f"code onto the person's computer — {finding.what}. Nothing was run.\n"
        f"Do this: 1) In the person's language (Russian: plain words, Elsa's voice) say in two "
        f"sentences what was about to be installed, that the plugin checks every outside install "
        f"first because packages get hijacked, and what happens next. 2) Vet it with this "
        f"plugin's `vet` skill (read ${{CLAUDE_PLUGIN_ROOT}}/skills/vet/SKILL.md): exact name, "
        f"version and source, age >= 14 days, fresh incident search, install scripts. 3) Verdict "
        f"`do-not-install` -> say so and offer another way; nothing is installed. Verdict "
        f"`clean` or `caution` -> tell the verdict and ask the person with buttons "
        f"(AskUserQuestion: «Ставим» / «Не ставим»). 4) Only after the person's own «Ставим» run: "
        f"vp guard approve {sid} --verdict <clean|caution> --report <path> (add --command '<safer "
        f"exact command>' if the vet pinned a version), then run that exact command once. Never "
        f"change the command after approval, never try to get around this guard, never turn it "
        f"off yourself.")


def terms_reason(what: str) -> str:
    return (
        f"AI Movie Company: the person has not accepted the plugin's terms yet, so it cannot "
        f"{what}. Nothing was run. Run `vp consent show`, show the short summary in the person's "
        f"language with the buttons «Принимаю» / «Прочитать полностью» / «Не сейчас» "
        f"(AskUserQuestion); only after their own «Принимаю» run `vp consent accept`, then retry. "
        f"«Не сейчас» -> `vp consent later` and do not install or publish anything.")


def protected_reason() -> str:
    return ("AI Movie Company: this touches the plugin's protected safety files (terms acceptance, "
            "install approvals, guard settings, mode, the record of the person's answers). Only "
            "the plugin's engine changes them, after the person's own answer: use `vp consent`, "
            "`vp guard`, `vp mode`. Reading them with cat/ls/grep is fine.")


def pretool(event: dict) -> dict | None:
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = str(event.get("cwd") or os.getcwd())
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = str(tin.get("file_path") or tin.get("notebook_path") or "")
        body = " ".join(str(tin.get(k) or "") for k in ("content", "new_string", "new_source"))
        if target:
            try:
                rp = Path(os.path.expanduser(target)).resolve()
                if any(rp == p.resolve() for p in protected_paths()):
                    return decision("deny", protected_reason())
            except OSError:
                pass
        if "AIMC_GUARD" in body:
            return decision("deny", protected_reason())
        return None
    if tool != "Bash":
        return None
    command = str(tin.get("command") or "")
    if not command.strip():
        return None
    if touches_protected(command, cwd):
        return decision("deny", protected_reason())
    # 1. the terms gate for the plugin's own installs and publishing
    needs = None
    if is_installer(command):
        needs = "install or update itself"
    for args in own_vp_calls(command, cwd):
        needs = needs or vp_needs_terms(args, cwd)
    if needs and not terms_accepted():
        return decision("deny", terms_reason(needs))
    # 2. the install guard
    if guard_on():
        finding = classify(command, cwd)
        if finding:
            if take_approval(command):
                if not terms_accepted():
                    return decision("deny", terms_reason("install anything"))
                return None                          # approved once: the normal permission flow
            sid = record_stop(command, finding, cwd)
            return decision("deny", stop_reason(sid, finding))
    # 3. fewer questions in Elsa's workspace (opt-in)
    if single_own_call(command, cwd) and preapproved_here(cwd):
        return decision("allow", "AI Movie Company: the plugin's own engine in Elsa's workspace "
                                 "(the person chose fewer questions; `vp mode undo` reverts)")
    return None


def preapproved_here(cwd: str) -> bool:
    m = read_json(vp_home() / "mode.json")
    if not m.get("enabled") or not m.get("workspace"):
        return False
    try:
        ws = Path(m["workspace"]).expanduser().resolve()
        here = Path(cwd).expanduser().resolve()
    except (OSError, TypeError):
        return False
    return here == ws or ws in here.parents


def prompt(event: dict) -> None:
    mode = event.get("permission_mode")
    text = event.get("prompt")
    if isinstance(text, str) and text.lstrip().startswith(("<task-notification", "<scheduled-task",
                                                           "<cross-session-message",
                                                           "<background-agent")):
        return
    sid = str(event.get("session_id") or "unknown")[:200]
    path = runtime_dir() / "session_modes.json"
    data = read_json(path)
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    rec = {"mode": mode if isinstance(mode, str) else None, "cwd": str(event.get("cwd") or ""),
           "at": iso(now())}
    sessions[sid] = rec
    if len(sessions) > MAX_SESSIONS:
        sessions = dict(sorted(sessions.items(), key=lambda kv: str(kv[1].get("at")))[-MAX_SESSIONS:])
    write_json(path, {"latest": dict(rec, session_id=sid), "sessions": sessions})


# ---- fail-safe: the decision crashed or ran out of time ------------------------------------------

class GuardDeadline(Exception):
    """The decision took longer than INTERNAL_DEADLINE_S."""


def _on_alarm(signum, frame):  # noqa: ARG001
    raise GuardDeadline(f"the check took longer than {INTERNAL_DEADLINE_S:g} s")


_PM = (r"(?:npm|pnpm|yarn|bun|pip[0-9.]*|pipx|uv|brew|port|cargo|gem|go|conda|mamba|micromamba|"
       r"apt|apt-get|yum|dnf|composer|poetry|deno|snap|flatpak|mas)")
_PM_VERBS = (r"(?:install|i|in|add|a|ci|clean-install|exec|x|dlx|update|up|upgrade|link|ln|"
             r"rebuild|create|download|wheel|inject|reinstall|tap|bundle|cask|binstall|get|"
             r"sync|require|global|create-project|localinstall|full-upgrade|dist-upgrade)")
_B = r"(?:^|[\s;&|(`/'\"])"               # the start of a word in a command line
_E = r"(?:\s|$|[;&|)])"                   # the end of it
_INSTALL_SHAPED = [re.compile(x, re.I) for x in (
    _B + _PM + r"\s+(?:-{1,2}[\w=./:-]+\s+(?:[^\s;&|-][^\s;&|]*\s+)?){0,4}" + _PM_VERBS + _E,
    _B + r"uv\s+(?:pip\s+(?:install|sync)|tool\s+(?:install|run|upgrade)|run\s+[^;&|]*--with)",
    _B + r"(?:go|pipx)\s+run" + _E,
    _B + r"yarn\s*(?:$|[;&|)])",                               # bare `yarn` installs
    _B + r"(?:npx|pnpx|bunx|uvx)\s",
    _B + r"-m\s+(?:pip|pipx|uv|poetry)\s+(?:install|download|wheel|add|run|inject)",
    _B + r"claude\s+(?:plugins?\s+(?:install|i|marketplace\s+add)|mcp\s+add)",
    r"(?:curl|wget|fetch|aria2c)\b[^|]*\|&?\s*(?:sudo\s+)?(?:env\s+)?(?:\w+=\S*\s+)*"
    r"(?:[a-z]*sh|python[0-9.]*|node|perl|ruby|php|osascript|eval|source)\b",
    # a download saved to a file and then run (errs toward yes: on this path safety wins)
    r"(?:curl|wget)\b[^;&|]*\s(?:-o|-O|-LO|-fLO|-sLO|--output|--output-document)\b.*?(?:&&|;|\|\|)"
    r"\s*(?:[a-z]*sh|python[0-9.]*|node|perl|ruby|chmod|open|installer|\./)",
    _B + r"(?:[a-z]*sh|eval|source|\.|python[0-9.]*|node|perl|ruby)\b[^;&|]*(?:\$\(|`|<\()\s*"
    r"(?:curl|wget)\b",
    r"(?:^|[\s;&|(])installer\s+[^;&|]*-pkg",
)]


def install_shaped(command: str) -> bool:
    """A coarse, crash-proof look at a command: could it bring outside code onto the machine?
    Used only when the real classifier failed; errs toward yes for install verbs."""
    text = command[:20000]
    return any(rx.search(text) for rx in _INSTALL_SHAPED)


def _guard_off_quiet() -> bool:
    try:
        return not guard_on()
    except BaseException:  # noqa: BLE001
        return (os.environ.get("AIMC_GUARD") or "").strip().lower() == "off"


def fail_safe_reason(err: BaseException) -> str:
    what = "timed out" if isinstance(err, GuardDeadline) else f"hit an internal error ({type(err).__name__})"
    return (
        f"AI Movie Company install guard: STOPPED to be safe. The guard {what} while checking this "
        f"command, and the command looks like it brings outside code onto the person's computer, "
        f"so it was not run. Do this: in the person's language (Russian: plain words, Elsa's "
        f"voice) say that the plugin's safety check broke on this command and that nothing was "
        f"installed. Retry once later; if the guard keeps failing, tell the person the way out: "
        f"quit Claude and start it again with AIMC_GUARD=off (Terminal: `AIMC_GUARD=off claude`), "
        f"or `vp guard off` after their own answer — and that turning the check off is their "
        f"choice, not yours. Never try to get around this guard yourself.")


def fail_safe(which: str, raw: str, err: BaseException) -> dict | None:
    """The decision when pretool() crashed or timed out (see the module docstring)."""
    if which != "pretool":
        return None
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    tool = str(event.get("tool_name") or "")
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = str(tin.get("file_path") or tin.get("notebook_path") or "")
        body = " ".join(str(tin.get(k) or "") for k in ("content", "new_string", "new_source"))
        if any(m in target for m in PROTECTED_MARKERS) or "AIMC_GUARD" in body:
            return decision("deny", protected_reason())
        return None
    if tool == "Bash" or not tool:
        command = str(tin.get("command") or "") if tool else raw      # unreadable event: look at it all
        if any(m in command for m in PROTECTED_MARKERS):
            return decision("deny", protected_reason())
        if install_shaped(command) and not _guard_off_quiet():
            return decision("deny", fail_safe_reason(err))
    return None


def _arm() -> bool:
    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, INTERNAL_DEADLINE_S)
        return True
    except (AttributeError, ValueError, OSError):      # no SIGALRM (Windows) or not main thread
        return False


def _disarm() -> None:
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (AttributeError, ValueError, OSError):
        pass


def main(argv: list[str]) -> int:
    which = argv[1] if len(argv) > 1 else ""
    raw = ""
    out = None
    try:
        _arm()
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            raise ValueError("the hook event is not a JSON object")
        if which == "pretool":
            out = pretool(event)
        elif which == "prompt":
            prompt(event)
        _disarm()
    except BaseException as err:  # noqa: BLE001 - a hook must never break the session
        _disarm()
        try:
            out = fail_safe(which, raw, err)
        except BaseException:  # noqa: BLE001 - the last line: no opinion
            out = None
    if out:
        try:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
        except BaseException:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
