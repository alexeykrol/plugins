"""RemotionRenderer — S5 / S7 through Remotion (SPEC R7, S5, S7; E4), behind `Renderer`.

Selected by config `renderer: remotion` (or a job's `design.yaml`, see vp.design). ffmpeg stays
the default. What Remotion draws, what ffmpeg still does:

  reel        Remotion: intro bumper, the clip with burned animated captions (word highlight)
              and the channel plate, outro bumper; ffmpeg joins the three and sets −14 LUFS.
  long/full   Remotion: intro and outro bumpers only. The long clip itself never goes through
              the browser (R7: its subtitles go to YouTube as a track) — ffmpeg joins the cut
              between the bumpers and sets loudness, as the ffmpeg renderer does.
  thumbnail   Remotion still (Cover) 1280×720; palette fallback when the PNG is ≥ 2 MB.

Counting stays in code (llm-as-component): this module turns subtitle cues into Remotion
`Caption` JSON (word times estimated by word length when only phrase times exist), decides the
caption pages, the card frame counts and every duration. The compositions in `<plugin>/remotion/`
only draw what the props say.

Layers for editors (contract `docs/LAYERS.md`): a final render also leaves
`clips/<id>/layers/` — `intro.mp4`, `outro.mp4` and `layers.yaml` with their places on the final
timeline; `vp design layers --captions` adds a transparent `captions.mov` (ProRes 4444).

Remotion runs from a private workspace `<vp_home>/remotion/<source hash>/` installed once with
`npm ci --ignore-scripts` from the pinned lockfile (docs/npm-supply-chain-audit-2026-11-18.md);
every remotion process is bound to 127.0.0.1 (scripts/localhost-only.cjs).
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - not on macOS/Linux
    fcntl = None  # type: ignore[assignment]

from ..core import progress
from ..core.errors import VPError
from ..core.io import write_yaml
from ..core.srt import Cue, read_srt
from . import media
from .render import KINDS, THUMB_H, THUMB_W, FfmpegRenderer, card_durations, readable_text_color, video_info

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REMOTION_SOURCE = PLUGIN_ROOT / "remotion"
# Files that make up the Remotion project (copied into the workspace). The dependency files name
# the workspace (a change = a new `npm ci`); the code files are re-copied when their hash changes.
DEP_FILES = ("package.json", "package-lock.json", ".npmrc")
CODE_FILES = ("tsconfig.json", "remotion.config.ts", "scripts/localhost-only.cjs")
SOURCE_FILES = DEP_FILES + CODE_FILES
SOURCE_DIRS = ("src",)
READY_MARK = ".vp-ready"
CODE_MARK = ".vp-code"
NODE_MIN_MAJOR = 18

REMOTION_DEFAULTS: dict[str, Any] = {
    "node_bin": None,          # None -> PATH, then ~/.nvm, /opt/homebrew, /usr/local
    "workspace": None,         # None -> <vp_home>/remotion
    "concurrency": None,       # None -> Remotion's default (half the cores)
    "browser_executable": None,  # None -> installed Google Chrome / Chromium (see find_browser)
    # Remotion's own browser download is not hash-checked (audit 2026-11-18, condition E): off
    # unless the owner turns it on; without a local Chrome the design path then says so plainly.
    "allow_browser_download": False,
    "gl": None,                # Chrome GL backend for Remotion (--gl): None = Remotion's default
    "crf": 16,                 # Remotion's own encode of the pieces (ffmpeg re-encodes the join)
    "timeout_s": 3600,
    "install_timeout_s": 900,
    "font_family": "-apple-system, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif",
    "accent_color": "#FFD23F",
    "captions": {
        "highlight": "word",   # word | phrase | none (none = no burned captions at all)
        "font_frac": 0.085,    # text size, fraction of the frame width
        "y_frac": 0.72,        # centre of the caption block, fraction of the height
        "plate": False,        # dark plate behind the words (else outline + shadow)
        "max_words": 3,        # words on one caption page
        "max_chars": 18,       # characters on one caption page (a longer single word stays alone)
    },
}

HIGHLIGHTS = ("word", "phrase", "none")


def remotion_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Config `remotion` over REMOTION_DEFAULTS."""
    from .render import _merge
    return _merge(REMOTION_DEFAULTS, (config or {}).get("remotion"))


# ---- captions: cues -> Remotion Caption JSON (code, deterministic) -----------------------------

def _words(text: str) -> list[str]:
    return " ".join((text or "").split()).split(" ") if (text or "").strip() else []


def cue_word_times(cue: Cue) -> list[tuple[str, int, int]]:
    """Split a cue into words with times. Only phrase times are known, so each word gets a share
    of the cue proportional to its length + 1 (a stand-in for syllables). Integer ms, contiguous,
    the last word ends exactly at the cue end."""
    words = _words(cue.text)
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    span = max(0, cue.end_ms - cue.start_ms)
    out, acc = [], 0
    for i, (w, wt) in enumerate(zip(words, weights)):
        start = cue.start_ms + round(span * acc / total)
        acc += wt
        end = cue.end_ms if i == len(words) - 1 else cue.start_ms + round(span * acc / total)
        out.append((w, start, end))
    return out


# Short function words that must not end a caption page when the phrase goes on ("в", "у",
# "и", "the"): a page ending on a preposition reads as a broken sentence.
_CLINGING = re.compile(r"^[\w\-]{1,2}$|^(?:без|для|про|над|под|при|что|как|the|and|for|with|from)$",
                       re.I | re.UNICODE)


def _clings(word: str) -> bool:
    return bool(_CLINGING.match(word)) and not re.search(r"[.,!?;:…»\")]$", word)


def _pages(words: list[str], max_words: int, max_chars: int) -> list[list[int]]:
    """Word indices of one cue split into pages (greedy by words/characters), then a clinging
    last word moves to the next page."""
    pages: list[list[int]] = []
    page: list[int] = []
    chars = 0
    for i, w in enumerate(words):
        if page and (len(page) >= max_words or chars + 1 + len(w) > max_chars):
            pages.append(page)
            page, chars = [], 0
        chars += len(w) + (1 if page else 0)
        page.append(i)
    if page:
        pages.append(page)
    for n in range(len(pages) - 1):
        while len(pages[n]) > 1 and _clings(words[pages[n][-1]]):
            pages[n + 1].insert(0, pages[n].pop())
    return pages


def captions_from_cues(cues: Iterable[Cue], *, max_words: int = 3, max_chars: int = 18) -> list[dict[str, Any]]:
    """Remotion `Caption[]` (@remotion/captions): one entry per word, leading space on every word
    that does not open a page, `pageBreakAfter` on the last word of each page. A page never
    crosses a cue; inside a cue it holds ≤ max_words words and ≤ max_chars characters (one long
    word may exceed it alone; a short preposition moved to the next page may add one word).
    Times are clip-relative ms; cues must not overlap."""
    if max_words < 1 or max_chars < 1:
        raise VPError("data", "captions: max_words and max_chars must be ≥ 1")
    out: list[dict[str, Any]] = []
    last_end = None
    for cue in cues:
        words = cue_word_times(cue)
        if not words:
            continue
        if last_end is not None and cue.start_ms < last_end:
            raise VPError("data", f"captions: cue {cue.id} overlaps the previous one")
        for page in _pages([w for w, _, _ in words], max_words, max_chars):
            for k, i in enumerate(page):
                w, s, e = words[i]
                out.append({"text": (" " + w) if k else w, "startMs": s, "endMs": e,
                            "timestampMs": (s + e) // 2, "confidence": None})
            out[-1]["pageBreakAfter"] = True
        last_end = cue.end_ms
    return out


def caption_violations(captions: list[dict[str, Any]], clip_ms: int) -> list[str]:
    """Code checks of the caption JSON: ordered, non-negative, inside the clip, non-empty words."""
    bad = []
    prev_end = 0
    for i, c in enumerate(captions):
        if not str(c.get("text", "")).strip():
            bad.append(f"{i}:empty")
        if c["startMs"] < prev_end or c["endMs"] < c["startMs"]:
            bad.append(f"{i}:order")
        if c["endMs"] > clip_ms + 1:
            bad.append(f"{i}:after_clip")
        prev_end = c["endMs"]
    return bad


# ---- node + workspace -------------------------------------------------------------------------

def _node_major(node: str) -> int | None:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"v(\d+)\.", out.strip())
    return int(m.group(1)) if m else None


def find_node(configured: str | None = None, home: str | os.PathLike | None = None) -> str | None:
    """Node binary: the configured one, the extra pack's (`vp extras`), then PATH, the newest nvm
    install, Homebrew."""
    from ..extras import node_path
    cands: list[str] = []
    if configured:
        cands.append(str(Path(configured).expanduser()))
    pack = node_path(home)
    if pack:
        cands.append(str(pack))
    w = shutil.which("node")
    if w:
        cands.append(w)
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        def ver(p: Path) -> tuple[int, ...]:
            return tuple(int(x) for x in re.findall(r"\d+", p.name)[:3]) or (0,)
        cands += [str(p / "bin" / "node") for p in sorted(nvm.iterdir(), key=ver, reverse=True)]
    cands += ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for c in cands:
        if Path(c).is_file() and (_node_major(c) or 0) >= NODE_MIN_MAJOR:
            return c
    return None


BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def find_browser(configured: str | None = None, home: str | os.PathLike | None = None) -> str | None:
    """A local Chrome for rendering: the configured one, the extra pack's Chrome Headless Shell,
    else the first installed Google Chrome / Chromium."""
    from ..extras import browser_path
    pack = browser_path(home)
    for c in ([configured] if configured else []) + ([str(pack)] if pack else []) + list(BROWSER_CANDIDATES):
        if c and Path(c).expanduser().is_file():
            return str(Path(c).expanduser())
    return None


@functools.lru_cache(maxsize=8)
def _missing_tools(node_bin: str | None, browser: str | None, allow_download: bool,
                   home: str | None = None) -> tuple[str, ...]:
    miss = []
    if not find_node(node_bin, home):
        miss.append(f"Node.js {NODE_MIN_MAJOR}+")
    if not find_browser(browser, home) and not allow_download:
        miss.append("a Chrome to draw in")
    return tuple(miss)


def missing_tools(config: dict[str, Any] | None) -> list[str]:
    """What this machine lacks for the design look, in plain words; [] when it is ready.
    Never raises; cached per process (one `node --version` per run, not per item)."""
    rs = remotion_settings(config)
    home = (config or {}).get("vp_home")
    return list(_missing_tools(rs.get("node_bin"), rs.get("browser_executable"),
                               bool(rs.get("allow_browser_download")), str(home) if home else None))


def effective_renderer(config: dict[str, Any] | None) -> tuple[str, str | None]:
    """The renderer that will actually draw: `remotion` falls back to `ffmpeg` when the tools are
    missing, with a note for the owner. S5 hashes the effective one, so installing the tools later
    re-renders the items made in the plain look."""
    kind = (config or {}).get("renderer", "ffmpeg")
    if kind != "remotion":
        return kind, None
    miss = missing_tools(config)
    if not miss:
        return "remotion", None
    return "ffmpeg", ("the design look needs the extra pack (missing here: " + ", ".join(miss)
                      + "); the video was made in the plain look for now — `vp extras install`")


def _hash(source: Path, files: Iterable[str], dirs: Iterable[str] = ()) -> str:
    h = hashlib.sha256()
    paths = [source / f for f in files]
    for d in dirs:
        paths += sorted(p for p in (source / d).rglob("*") if p.is_file())
    for p in paths:
        if not p.is_file():
            raise VPError("internal", f"Remotion project file missing: {p}")
        h.update(str(p.relative_to(source)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def source_hash(source: Path = REMOTION_SOURCE) -> str:
    """Short hash of the dependency pins: names the workspace."""
    return _hash(source, DEP_FILES)


def code_hash(source: Path = REMOTION_SOURCE) -> str:
    """Short hash of the compositions and configs: re-copied without reinstalling."""
    return _hash(source, CODE_FILES, SOURCE_DIRS)


class RemotionWorkspace:
    """`<vp_home>/remotion/<hash>/`: a copy of the plugin's Remotion project with node_modules.
    Installed once (`npm ci --ignore-scripts`, pinned lockfile); never inside the plugin folder."""

    def __init__(self, config: dict[str, Any] | None = None, home: str | os.PathLike | None = None):
        self.settings = remotion_settings(config)
        base = self.settings.get("workspace") or Path(home or (config or {}).get("vp_home")
                                                      or Path.home() / ".ai_video_pipeline") / "remotion"
        self.base = Path(base).expanduser()
        self.source = REMOTION_SOURCE
        self.dir = self.base / source_hash(self.source)
        self.home = home or (config or {}).get("vp_home")
        self._node: str | None = None

    @property
    def node(self) -> str:
        if self._node is None:
            n = find_node(self.settings.get("node_bin"), self.home)
            if not n:
                raise VPError("internal", f"Node.js {NODE_MIN_MAJOR}+ not found (needed for the design "
                                          f"look): install the extra pack — `vp extras install`")
            self._node = n
        return self._node

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        node_dir = str(Path(self.node).parent)
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        guard = str(self.dir / "scripts" / "localhost-only.cjs")
        # Node splits NODE_OPTIONS on spaces unless the value is double-quoted (\" and \\ escaped).
        quoted = '"' + guard.replace("\\", "\\\\").replace('"', '\\"') + '"'
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + f" --require {quoted}").strip()
        env["REMOTION_DISABLE_TELEMETRY"] = "1"
        env.setdefault("npm_config_update_notifier", "false")
        return env

    @contextlib.contextmanager
    def lock(self, exclusive: bool) -> Iterator[None]:
        """One lock file for all workspaces under `base`: renders hold it shared; install, code
        refresh and pruning hold it exclusive, so no job swaps files under a running render."""
        self.base.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        fd = os.open(self.base / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def current(self) -> bool:
        """Installed and its code matches the plugin's."""
        mark = self.dir / CODE_MARK
        return self.ready() and mark.is_file() and mark.read_text().strip() == code_hash(self.source)

    def ready(self) -> bool:
        return (self.dir / READY_MARK).is_file() and (self.cli_js()).is_file()

    def cli_js(self) -> Path:
        return self.dir / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"

    def _copy_code(self, dst: Path) -> None:
        for f in CODE_FILES:
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source / f, dst / f)
        for d in SOURCE_DIRS:
            if (dst / d).exists():
                shutil.rmtree(dst / d)
            shutil.copytree(self.source / d, dst / d)
        (dst / CODE_MARK).write_text(code_hash(self.source) + "\n")

    def ensure(self, log=print) -> Path:
        """Install the workspace if needed (network, ~1–2 min the first time); refresh the code.
        Waits for running renders (shared lock holders) before changing any file."""
        if self.current():
            return self.dir
        with self.lock(exclusive=True):
            if self.current():          # another job did it while we waited
                return self.dir
            if self.ready():
                self._copy_code(self.dir)
                return self.dir
            return self._install(log)

    def _install(self, log) -> Path:
        npm = Path(self.node).parent / "npm"
        if not npm.exists():
            raise VPError("internal", f"npm not found next to {self.node}")
        self.base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.base))
        try:
            for f in DEP_FILES:
                shutil.copy2(self.source / f, staging / f)
            self._copy_code(staging)
            log("Installing the Remotion design tools (once, about a minute)…")
            env = self.env()
            env["NODE_OPTIONS"] = ""
            proc = subprocess.run([str(npm), "ci", "--ignore-scripts", "--omit=dev", "--no-audit",
                                   "--no-fund", "--loglevel=error"], cwd=staging, env=env, capture_output=True,
                                  text=True, timeout=int(self.settings["install_timeout_s"]))
            if proc.returncode != 0:
                raise VPError("transport", f"npm ci failed: {(proc.stderr or proc.stdout).strip()[-600:]}")
            (staging / READY_MARK).write_text(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            if self.dir.exists():
                shutil.rmtree(self.dir)
            os.replace(staging, self.dir)
            self._prune()
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return self.dir

    def _prune(self) -> None:
        """Workspaces of older plugin versions (~500 MB each) go once the new one is ready."""
        for d in self.base.iterdir():
            if d.is_dir() and d != self.dir and re.fullmatch(r"[0-9a-f]{12}", d.name):
                shutil.rmtree(d, ignore_errors=True)

    _FRAMES_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

    def browser_args(self) -> list[str]:
        b = find_browser(self.settings.get("browser_executable"), self.home)
        if b:
            mode = "headless-shell" if Path(b).name == "chrome-headless-shell" else "chrome-for-testing"
            return [f"--browser-executable={b}", f"--chrome-mode={mode}"]
        if self.settings.get("allow_browser_download"):
            return []
        raise VPError("internal", "no Chrome to draw the video in: install the extra pack "
                                  "(`vp extras install`) or Google Chrome, then try again.")

    def run(self, args: list[str], *, timeout_s: float | None = None, frames: int | None = None) -> str:
        """Run the Remotion CLI in the workspace; reports frame progress when a tracker is active."""
        self.ensure()
        extra = self.browser_args() if args and args[0] in ("render", "still", "studio") else []
        cmd = [self.node, str(self.cli_js()), *[str(a) for a in args], *extra]
        timeout_s = float(timeout_s or self.settings["timeout_s"])
        with self.lock(exclusive=False):
            if not self.current():      # changed between ensure() and the shared lock
                raise VPError("transport", "the design tools were being updated by another job; "
                                           "run again")
            rc, lines = self._watch(cmd, timeout_s, frames)
        text = "".join(lines)
        if rc != 0:
            # Stack frames say nothing to anyone; keep the lines that name the error.
            said = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("at ")]
            raise VPError("data", f"remotion {args[0]} failed: {' | '.join(said[-6:])[-800:]}")
        return text


    def _watch(self, cmd: list[str], timeout_s: float, frames: int | None) -> tuple[int, list[str]]:
        """Run with a watchdog: a process that hangs silently (Chrome stuck, no output) is killed
        at the deadline too, not only when it next prints."""
        since = progress.clock()
        proc = subprocess.Popen(cmd, cwd=self.dir, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            try:
                os.killpg(proc.pid, 9)  # node and the Chrome it started
            except (OSError, AttributeError):
                proc.kill()
        watchdog = threading.Timer(timeout_s, kill)
        watchdog.daemon = True
        watchdog.start()
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                if len(lines) > 400:
                    del lines[:200]
                if frames:
                    for m in self._FRAMES_RE.finditer(line):
                        done, total = int(m.group(1)), int(m.group(2))
                        if total == frames and done <= total:
                            progress.report(done, total, "frames", since=since)
            rc = proc.wait()
        finally:
            watchdog.cancel()
            if proc.stdout:
                proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if timed_out.is_set():
            raise VPError("data", f"remotion timed out after {timeout_s:.0f}s")
        return rc, lines


# ---- the renderer --------------------------------------------------------------------------

def _link(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        os.symlink(Path(src).resolve(), dst)


class RemotionRenderer(FfmpegRenderer):
    """Renderer on Remotion (bumpers, reel captions, cover) + ffmpeg (join, loudness).
    Inherits brand/render settings and helpers from FfmpegRenderer."""

    def __init__(self, config: dict[str, Any] | None = None, workspace: RemotionWorkspace | None = None):
        super().__init__(config)
        self.rs = remotion_settings(self.config)
        self.workspace = workspace or RemotionWorkspace(self.config)
        self.timings: dict[str, float] = {}

    # ---- props -----------------------------------------------------------------------------
    def brand_props(self) -> dict[str, Any]:
        b = self.brand
        return {
            "fontFamily": self.rs["font_family"],
            "textColor": readable_text_color(b["text_color"], b["plate_color"]),
            "plateColor": b["plate_color"],
            "cardBg": b["card_bg"],
            "cardTextColor": readable_text_color(b["card_text_color"], b["card_bg"]),
            "accentColor": self.rs["accent_color"],
            "channelPlate": b.get("channel_plate") or None,
        }

    def caption_style(self) -> dict[str, Any]:
        c = self.rs["captions"]
        hl = c.get("highlight", "word")
        if hl not in HIGHLIGHTS:
            raise VPError("data", f"remotion.captions.highlight must be one of {list(HIGHLIGHTS)}, got {hl!r}")
        return {"highlight": "phrase" if hl == "none" else hl, "fontFrac": float(c["font_frac"]),
                "yFrac": float(c["y_frac"]), "plate": bool(c["plate"])}

    def captions(self, clip_srt: str | os.PathLike, clip_ms: int) -> list[dict[str, Any]]:
        c = self.rs["captions"]
        if c.get("highlight") == "none":
            return []
        caps = captions_from_cues(read_srt(clip_srt), max_words=int(c["max_words"]),
                                  max_chars=int(c["max_chars"]))
        bad = caption_violations(caps, clip_ms)
        if bad:
            raise VPError("data", f"captions invalid: {bad[:5]}")
        return caps

    # ---- remotion calls -----------------------------------------------------------------
    def _render(self, comp: str, props: dict[str, Any], out: Path, tmp: Path, public: Path | None,
                frames: int, extra: list[str] | None = None) -> Path:
        pf = tmp / f"{comp}-{out.stem}.props.json"
        pf.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        args = ["render", "src/index.ts", comp, str(out), f"--props={pf}", "--log=info"]
        if not any(a.startswith("--codec=") for a in extra or []):  # CRF is for H.264 only
            args += [f"--crf={int(self.rs['crf'])}", "--enforce-audio-track"]
        if public:
            args.append(f"--public-dir={public}")
        if self.rs.get("concurrency"):
            args.append(f"--concurrency={self.rs['concurrency']}")
        if self.rs.get("gl"):
            args.append(f"--gl={self.rs['gl']}")
        args += extra or []
        t0 = time.monotonic()
        self.workspace.run(args, frames=frames)
        self.timings[f"{comp}:{out.stem}"] = round(time.monotonic() - t0, 2)
        if not out.is_file():
            raise VPError("data", f"remotion wrote no {out.name}")
        return out

    def render_bumper(self, role: str, text: str, w: int, h: int, fps: float, frames: int, out: Path,
                      tmp: Path) -> Path:
        props = {"width": w, "height": h, "fps": fps, "durationInFrames": frames, "role": role,
                 "text": text or "", "brand": self.brand_props(), "showPlate": role == "outro"}
        return self._render("Bumper", props, out, tmp, None, frames)

    def render_captions_layer(self, cut_mp4: str | os.PathLike, clip_srt: str | os.PathLike,
                              out_mov: str | os.PathLike) -> Path:
        """Transparent captions + plate over the clip timeline (ProRes 4444) for an editor's track."""
        info = video_info(cut_mp4)
        clip_ms = info["video_ms"] or media.duration_ms(cut_mp4)
        frames = max(1, round(clip_ms * info["fps"] / 1000))
        tmp = Path(tempfile.mkdtemp(prefix="vp-remotion-"))
        try:
            props = {"width": info["width"], "height": info["height"], "fps": info["fps"],
                     "durationInFrames": frames, "video": None,
                     "captions": self.captions(clip_srt, clip_ms), "captionStyle": self.caption_style(),
                     "brand": self.brand_props(), "showPlate": True}
            out = Path(out_mov)
            out.parent.mkdir(parents=True, exist_ok=True)
            part = tmp / "captions.mov"
            self._render("Main", props, part, tmp, None, frames,
                         ["--codec=prores", "--prores-profile=4444", "--image-format=png",
                          "--pixel-format=yuva444p10le"])
            os.replace(part, out) if part.stat().st_dev == out.parent.stat().st_dev else shutil.move(part, out)
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- interface ------------------------------------------------------------------------
    def render_clip(self, cut_mp4, clip_srt, design_texts, kind, out_mp4, *,
                    layers_dir: str | os.PathLike | None | bool = True) -> dict[str, int]:
        """layers_dir: True = `<out dir>/layers` when out is render.mp4 (a real S5 render),
        a path = there, None/False = no layers (previews)."""
        if kind not in KINDS:
            raise VPError("data", f"render: unknown kind {kind!r}")
        self.timings = {}
        t_all = time.monotonic()
        info = video_info(cut_mp4)
        if info["audio_ms"] is None:
            raise VPError("data", "render: cut has no audio")
        w, h, fps = info["width"], info["height"], info["fps"]
        want_in, want_out = card_durations(self.config, kind)
        in_frames, out_frames = round(want_in * fps / 1000), round(want_out * fps / 1000)
        intro_ms, outro_ms = round(in_frames * 1000 / fps), round(out_frames * 1000 / fps)
        clip_ms = info["video_ms"] or media.duration_ms(cut_mp4)
        clip_frames = round(clip_ms * fps / 1000)
        total_s = (in_frames + clip_frames + out_frames) / fps
        texts = design_texts or {}
        out = Path(out_mp4)
        if layers_dir is True:
            layers = out.parent / "layers" if out.name == "render.mp4" else None
        else:
            layers = Path(layers_dir) if layers_dir else None
        tmp = Path(tempfile.mkdtemp(prefix="vp-remotion-"))
        try:
            parts: list[tuple[Path, str]] = []
            if in_frames > 0:
                parts.append((self.render_bumper("intro", str(texts.get("intro_text") or ""), w, h, fps,
                                                 in_frames, tmp / "intro.mp4", tmp), "intro"))
            if kind == "reel":
                public = tmp / "public"
                public.mkdir()
                _link(Path(cut_mp4), public / "cut.mp4")
                props = {"width": w, "height": h, "fps": fps, "durationInFrames": clip_frames,
                         "video": "cut.mp4", "captions": self.captions(clip_srt, clip_ms),
                         "captionStyle": self.caption_style(), "brand": self.brand_props(),
                         "showPlate": True}
                parts.append((self._render("Main", props, tmp / "main.mp4", tmp, public, clip_frames), "clip"))
            else:
                parts.append((Path(cut_mp4), "clip"))
            if out_frames > 0:
                text = str(texts.get("announce_text") or texts.get("outro_text") or "")
                parts.append((self.render_bumper("outro", text, w, h, fps, out_frames, tmp / "outro.mp4", tmp),
                              "outro"))
            t0 = time.monotonic()
            self._join(parts, w, h, info["rate"], total_s, out, tmp)
            self.timings["ffmpeg:join+loudnorm"] = round(time.monotonic() - t0, 2)
            if layers is not None:
                # a new final resets the layers; a captions track is added on request only
                (layers / "captions.mov").unlink(missing_ok=True)
                self._write_layers(layers, parts, out.parent.name, "../" + out.name, kind, info,
                                   intro_ms, clip_ms, outro_ms)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.timings["total"] = round(time.monotonic() - t_all, 2)
        return {"intro_ms": intro_ms, "outro_ms": outro_ms, "duration_ms": media.duration_ms(out)}

    def _join(self, parts: list[tuple[Path, str]], w: int, h: int, rate: str, total_s: float,
              out: Path, tmp: Path) -> None:
        """Concatenate the pieces (re-encode, same size/rate/48 kHz stereo), then two-pass loudnorm."""
        afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        inputs, chains, segs = [], [], []
        for n, (p, _) in enumerate(parts):
            inputs += ["-i", str(p)]
            chains.append(f"[{n}:v]scale={w}:{h},fps={rate},format=yuv420p,setsar=1[v{n}]")
            chains.append(f"[{n}:a]{afmt}[a{n}]")
            segs.append(f"[v{n}][a{n}]")
        chains.append("".join(segs) + f"concat=n={len(parts)}:v=1:a=1[v][a]")
        joined = tmp / "joined.mp4"
        media.run_ffmpeg([*inputs, "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
                          *media.VIDEO_ENC, "-color_primaries", "bt709", "-color_trc", "bt709",
                          "-colorspace", "bt709", *media.AUDIO_ENC, str(joined)],
                         progress_ms=round(total_s * 1000))
        st = self.settings
        m = media.loudnorm_measure(joined, st["lufs"], st["true_peak"], st["lra"])
        af = (f"loudnorm=I={st['lufs']}:TP={st['true_peak']}:LRA={st['lra']}:"
              f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
              f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
              f"aresample=48000,atrim=duration={total_s:.6f}")
        self._to(out, lambda t: media.run_ffmpeg(
            ["-i", str(joined), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-af", af,
             "-ar", "48000", *media.AUDIO_ENC, "-movflags", "+faststart", t]))

    def render_layers(self, cut_mp4, clip_srt, design_texts, kind, layers_dir, *, captions: bool = False,
                      final: str | None = None) -> Path:
        """Editor layers without a final render (an item rendered by ffmpeg, or before S5):
        the bumpers, optionally the transparent captions track, and layers.yaml. Returns it."""
        if kind not in KINDS:
            raise VPError("data", f"render: unknown kind {kind!r}")
        info = video_info(cut_mp4)
        w, h, fps = info["width"], info["height"], info["fps"]
        want_in, want_out = card_durations(self.config, kind)
        in_frames, out_frames = round(want_in * fps / 1000), round(want_out * fps / 1000)
        intro_ms, outro_ms = round(in_frames * 1000 / fps), round(out_frames * 1000 / fps)
        clip_ms = info["video_ms"] or media.duration_ms(cut_mp4)
        texts = design_texts or {}
        layers = Path(layers_dir)
        layers.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="vp-remotion-"))
        try:
            parts: list[tuple[Path, str]] = []
            if in_frames > 0:
                parts.append((self.render_bumper("intro", str(texts.get("intro_text") or ""), w, h, fps,
                                                 in_frames, tmp / "intro.mp4", tmp), "intro"))
            parts.append((Path(cut_mp4), "clip"))
            if out_frames > 0:
                text = str(texts.get("announce_text") or texts.get("outro_text") or "")
                parts.append((self.render_bumper("outro", text, w, h, fps, out_frames, tmp / "outro.mp4", tmp),
                              "outro"))
            cap = layers / "captions.mov"
            if captions:
                self.render_captions_layer(cut_mp4, clip_srt, cap)
            elif cap.exists():
                cap.unlink()
            self._write_layers(layers, parts, Path(cut_mp4).parent.name, final, kind, info,
                               intro_ms, clip_ms, outro_ms)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return layers / "layers.yaml"

    def _write_layers(self, layers: Path, parts: list[tuple[Path, str]], item: str, final: str | None,
                      kind: str, info: dict[str, Any], intro_ms: int, clip_ms: int, outro_ms: int) -> None:
        """Editor layers (docs/LAYERS.md): bumper files + layers.yaml on the final timeline."""
        layers.mkdir(parents=True, exist_ok=True)
        tracks = []
        start = {"intro": 0, "clip": intro_ms, "outro": intro_ms + clip_ms}
        dur = {"intro": intro_ms, "clip": clip_ms, "outro": outro_ms}
        for p, role in parts:
            if role == "clip":
                tracks.append({"role": "clip", "file": "../cut.mp4", "start_ms": start["clip"],
                               "duration_ms": clip_ms, "alpha": False, "audio": "speech",
                               "note": "the cut without captions or plate"})
                continue
            dst = layers / f"{role}.mp4"
            shutil.copyfile(p, dst)
            tracks.append({"role": role, "file": dst.name, "start_ms": start[role], "duration_ms": dur[role],
                           "alpha": False, "audio": "silent"})
        cap = layers / "captions.mov"
        if cap.is_file():
            tracks.append({"role": "captions", "file": cap.name, "start_ms": intro_ms, "duration_ms": clip_ms,
                           "alpha": True, "audio": None})
        write_yaml(layers / "layers.yaml", {
            "version": 1, "item": item, "kind": kind, "renderer": "remotion",
            "final": final, "timeline": "final", "width": info["width"],
            "height": info["height"], "fps": info["fps"], "duration_ms": intro_ms + clip_ms + outro_ms,
            "tracks": tracks})

    def render_thumbnail(self, frame_png, text, out_png) -> None:
        fg, plate = self.thumbnail_colors()
        tmp = Path(tempfile.mkdtemp(prefix="vp-remotion-"))
        try:
            public = tmp / "public"
            public.mkdir()
            _link(Path(frame_png), public / "frame.png")
            props = {"frame": "frame.png", "text": text or "", "brand": self.brand_props(), "textOnPlate": fg}
            pf = tmp / "cover.props.json"
            pf.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
            still = tmp / "cover.png"
            t0 = time.monotonic()
            self.workspace.run(["still", "src/index.ts", "Cover", str(still), f"--props={pf}",
                                f"--public-dir={public}", "--image-format=png", "--log=info"])
            self.timings["Cover"] = round(time.monotonic() - t0, 2)
            limit = int(self.settings["thumb_max_bytes"])

            def run(t: str) -> None:
                shutil.copyfile(still, t)
                if os.path.getsize(t) >= limit:
                    media.run_ffmpeg(["-i", str(still), "-filter_complex",
                                      f"scale={THUMB_W}:{THUMB_H},split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse",
                                      "-frames:v", "1", "-update", "1", "-f", "image2", "-c:v", "png", t])
                if os.path.getsize(t) >= limit:
                    raise VPError("data", f"thumbnail is {os.path.getsize(t)} bytes, limit {limit}")
            self._to(Path(out_png), run)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
