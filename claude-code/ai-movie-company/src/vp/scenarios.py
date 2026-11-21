"""Scenario tree: whatever a person comes in with, the route is covered (owner, 2026-11-20).

Data, not prose: `vp/knowledge/scenarios.json` (scenarios A–N of docs/SCENARIOS.md, their ordered
steps, cost constants with sources, intake checklists by kind of video) and
`vp/knowledge/capabilities.json` (capability codes П Р Ч К Б М С Г ГВ А Д Ф Пл Т and the built ones:
provider, service group, how presence is detected, built / partial / planned). This module only
counts (SPEC §0, llm-as-component): which steps can run with the services at hand, which fall back
to a degraded path, what it costs. Judgment — which scenario the person really means — stays with
the director in the dialog; `detect` gives ranked candidates and the one question that splits the
top two.

Availability of a step = every capability it needs is built (or partial) AND its service group is
connected. A planned capability is unavailable today; `--assume-built` (and the matrix "designed"
view) treats it as available when its group is connected — what the services will unlock once the
adapters land.

Service presence is never read from secret values: only whether an env var is set, a token file
exists, or the animation pack is ready. ElevenLabs is detected by its saved key (one key for
recognition, voice and music). The claude.ai ElevenLabs connector, a fallback only, is invisible to
the engine: the skill adds `elevenlabs` to `--services` when there is no key but the connector's
tools are in the session.

CLI:
  vp scenario list [--json]
  vp scenario detect <job-folder|file> [--says "<the person's words>"] [--no-probe] [--json]
  vp scenario plan <id> [--minutes N] [--source-hours H] [--images K] [--ai-video-seconds S]
                        [--services auto|none|<group>,...] [--assume-built] [--kind K] [--json]
  vp scenario matrix [--json] [--write <file.md>]
  vp scenario services [--json]          what `--services auto` sees on this machine
  vp scenario check [--json]             data validity (exit 2 on problems)
"""
from __future__ import annotations

import argparse
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .core.errors import VPError, note_failure

KNOWLEDGE = Path(__file__).resolve().parent / "knowledge"
SCENARIOS_FILE = KNOWLEDGE / "scenarios.json"
CAPABILITIES_FILE = KNOWLEDGE / "capabilities.json"

EXECUTORS = ("code", "model", "human_gate")
AVAILABLE_STATUSES = ("built", "partial")
STATUSES = ("built", "partial", "planned")
MATRIX_MINUTES = (1, 5, 10)
# the owner's service sets (2026-11-20); free assumed groups (Claude, animation pack, YouTube) are in every set
MATRIX_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bare", ()),
    ("+elevenlabs", ("elevenlabs",)),
    ("+elevenlabs+images", ("elevenlabs", "images")),
    ("+elevenlabs+images+video", ("elevenlabs", "images", "video")),
    ("all", ("*",)),
)
REGEN = "vp scenario matrix --write docs/scenario-matrix.md"

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".mpg", ".mpeg")
SUB_EXT = (".srt", ".vtt")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".tif", ".tiff")
TEXT_EXT = (".txt", ".md", ".rtf", ".docx", ".doc", ".pdf", ".odt", ".pages")
READABLE_TEXT = (".txt", ".md")
# pipeline outputs and tool folders are not the person's material
SKIP_DIRS = {"clips", "stages", "package", "full", "exports", "capcut", "voice", "sketches", "layers",
             "renders", "render", "judge", "node_modules", "__pycache__", "out", "public"}
SKIP_FILES = {"request.yaml", "status.yaml", "job.yaml", "script.md", "brief.md", "report.md", "asks.md",
              "asks.yaml", "approvals.yaml", "design.yaml", "journal.jsonl", "job.lock"}
MAX_FILES = 2000
MAX_DEPTH = 3
MAX_PROBES = 10
IDEA_MAX_CHARS = 600
AMBIGUOUS_MARGIN = 20
WEAK_SCORE = 40
SAYS_KEYWORD = 35
TEXT_KEYWORD = 12
SCRIPT_MARKERS = re.compile(
    r"(?im)^\s*(?:\*\*)?(?:диктор|голос за кадром|закадр\w*|vo|v\.o\.|narrator|сцена\s*\d+|scene\s*\d+|"
    r"int\.|ext\.|кадр\s*\d+|титр\s*:)")


# ---- data ---------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _raw() -> tuple[dict[str, Any], dict[str, Any]]:
    return (json.loads(SCENARIOS_FILE.read_text(encoding="utf-8")),
            json.loads(CAPABILITIES_FILE.read_text(encoding="utf-8")))


def tree() -> dict[str, Any]:
    return _raw()[0]


def registry() -> dict[str, Any]:
    return _raw()[1]


def capabilities() -> dict[str, dict[str, Any]]:
    return registry()["capabilities"]


def groups() -> dict[str, dict[str, Any]]:
    return registry()["groups"]


def assumed_groups() -> set[str]:
    return {g for g, d in groups().items() if d.get("assumed")}


def resolve_step(step: dict[str, Any]) -> dict[str, Any]:
    """{"use": lib id, ...overrides} -> the library step with the overrides; id defaults to the lib id."""
    if "use" not in step:
        return dict(step)
    lib = tree()["lib"]
    if step["use"] not in lib:
        raise VPError("data", f"scenario step uses unknown library step {step['use']!r}")
    out = dict(lib[step["use"]])
    out["id"] = step["use"]
    out.update({k: v for k, v in step.items() if k != "use"})
    return out


def scenario_list() -> list[dict[str, Any]]:
    return tree()["scenarios"]


def scenario(sid: str) -> dict[str, Any]:
    for s in scenario_list():
        if s["id"].upper() == str(sid).strip().upper():
            return s
    raise VPError("data", f"unknown scenario {sid!r} (known: {', '.join(s['id'] for s in scenario_list())})")


def steps_of(s: dict[str, Any]) -> list[dict[str, Any]]:
    return [resolve_step(st) for st in s["steps"]]


# ---- services -----------------------------------------------------------------------------------

def _config_file_exists(dotted: str, home: Path | None) -> bool:
    from .core.config import config_path
    from .core.io import read_yaml
    p = config_path(Path(home) if home is not None else None)
    if not Path(p).is_file():
        return False
    node: Any = read_yaml(Path(p), default={}) or {}
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return bool(node) and Path(str(node)).expanduser().is_file()


def _extras_ready(home: Path | None) -> bool:
    try:
        from .extras import design_ready
        return bool(design_ready(home))
    except Exception:  # noqa: BLE001 — detection never crashes
        return False


def _has_key(service: str, home: Path | None) -> bool:
    """Is a key saved for `service`? Only presence; the value is never returned."""
    from .keys import get_key   # elevenlabs included (its older key file is migrated on read)
    return bool(get_key(service, home))


def detect_groups(home: Path | None = None, env: dict[str, str] | None = None,
                  extras_ready: Callable[[Path | None], bool] | None = None,
                  config_file: Callable[[str, Path | None], bool] | None = None) -> dict[str, str]:
    """{group: why it counts as connected}. Never reads a secret's value beyond 'set or not'."""
    env = os.environ if env is None else env
    extras_ready = extras_ready or _extras_ready
    config_file = config_file or _config_file_exists
    found: dict[str, str] = {}
    for g, d in groups().items():
        det = d.get("detect") or {}
        if d.get("always"):
            found[g] = "always"
            continue
        for name in det.get("env") or ():
            if env.get(name):
                found[g] = f"env {name} is set"
                break
        for svc in det.get("keys") or ():
            if g in found:
                break
            try:
                if _has_key(svc, home):
                    found[g] = f"{svc} key saved"
            except Exception:  # noqa: BLE001 — detection never crashes
                pass
        if g not in found and det.get("config_file"):
            try:
                if config_file(det["config_file"], home):
                    found[g] = f"config {det['config_file']} exists"
            except Exception:  # noqa: BLE001
                pass
        if g not in found and det.get("extras") and extras_ready(home):
            found[g] = "animation pack ready"
    return found


def parse_services(spec: str | None, detect: Callable[[], dict[str, str]] | None = None) -> dict[str, str]:
    """`auto` (default) | `none` | comma list of groups, `auto` may be combined: `auto,elevenlabs`."""
    tokens = [t.strip().lower() for t in (spec or "auto").split(",") if t.strip()]
    known = set(groups())
    out: dict[str, str] = {}
    if "auto" in tokens:
        out.update((detect or detect_groups)())
    else:
        out.update({g: "assumed (free)" for g in assumed_groups()})
    for t in tokens:
        if t in ("auto", "none"):
            continue
        if t in ("all", "*"):
            out.update({g: "given" for g in known if g not in out})
            continue
        if t not in known:
            raise VPError("data", f"unknown service {t!r} (known: auto, none, all, {', '.join(sorted(known))})")
        out.setdefault(t, "given")
    out.setdefault("claude", "always")
    return out


# ---- planning -----------------------------------------------------------------------------------

def cap_available(cap_id: str, present: Iterable[str], assume_built: bool = False) -> bool:
    cap = capabilities()[cap_id]
    built = cap["status"] in AVAILABLE_STATUSES or assume_built
    return built and cap["group"] in set(present)


def _missing(needs: Iterable[str], present: set[str], assume_built: bool) -> dict[str, list[str]]:
    caps = capabilities()
    not_built = [c for c in needs if caps[c]["status"] not in AVAILABLE_STATUSES and not assume_built]
    services = sorted({caps[c]["group"] for c in needs if caps[c]["group"] not in present})
    return {"not_built": not_built, "services": services}


def _const(name: str) -> tuple[float, float]:
    c = tree()["constants"][name]
    return float(c["low"]), float(c["high"])


def quantity(name: str, s: dict[str, Any], q: dict[str, Any]) -> tuple[float, float]:
    m = float(q.get("minutes") or 1)
    if name == "output_min":
        return m, m
    if name == "fixed":
        return 1.0, 1.0
    if name == "images":
        if q.get("images") is not None:
            return float(q["images"]), float(q["images"])
        lo, hi = _const("images_per_min")
        return m * lo, m * hi
    if name == "ai_video_s":
        if q.get("ai_video_seconds") is not None:
            return float(q["ai_video_seconds"]), float(q["ai_video_seconds"])
        lo, hi = _const("ai_video_s_per_min")
        return m * lo, m * hi
    if name == "source_hours":
        if q.get("source_hours") is not None:
            return float(q["source_hours"]), float(q["source_hours"])
        d = (s.get("defaults") or {}).get("source_hours") or {}
        if "by_output" in d:
            rows = d["by_output"]
            pick = next((r[1] for r in rows if m <= r[0]), rows[-1][1])
            return float(pick[0]), float(pick[1])
        lo, hi = d.get("ratio_to_output", [1, 1])
        return m * lo / 60.0, m * hi / 60.0
    raise VPError("data", f"unknown cost quantity {name!r}")


def step_cost(cost: dict[str, Any] | None, s: dict[str, Any], q: dict[str, Any]) -> tuple[float, float]:
    if not cost:
        return 0.0, 0.0
    lo, hi = quantity(cost["qty"], s, q)
    for f in cost.get("factors") or ():
        flo, fhi = _const(f)
        lo, hi = lo * flo, hi * fhi
    return lo, hi


def _view(step: dict[str, Any]) -> dict[str, Any]:
    return {"title": step["title"], "executor": step["executor"], "command": step.get("command")}


def plan(sid: str, *, minutes: float = 1, source_hours: float | None = None, images: int | None = None,
         ai_video_seconds: float | None = None, present: Iterable[str] = (), assume_built: bool = False,
         kind: str | None = None) -> dict[str, Any]:
    """Ordered steps with the path each takes given the connected groups, cost low/high, status."""
    s = scenario(sid)
    present = set(present) | {"claude"}
    q = {"minutes": minutes, "source_hours": source_hours, "images": images, "ai_video_seconds": ai_video_seconds}
    out_steps: list[dict[str, Any]] = []
    total_lo = total_hi = 0.0
    status = "full"
    record_demand = False
    connect: set[str] = set()
    later: set[str] = set()
    for st in steps_of(s):
        needs = st.get("needs") or []
        optional = bool(st.get("optional"))
        miss = _missing(needs, present, assume_built)
        main_ok = not miss["not_built"] and not miss["services"]
        row: dict[str, Any] = {"id": st["id"], "title": st["title"], "executor": st["executor"],
                               "command": st.get("command"), "needs": needs, "optional": optional}
        if st.get("stage"):
            row["stage"] = st["stage"]
        if main_ok:
            row["path"] = "main"
            lo, hi = step_cost(st.get("cost"), s, q)
        else:
            row["missing"] = miss
            fb = st.get("fallback")
            fb_ok = bool(fb) and not any(not cap_available(c, present, assume_built) for c in fb.get("needs") or [])
            if miss["not_built"] and not optional:
                record_demand = True
            if miss["not_built"]:
                later.update(miss["not_built"])
            elif miss["services"]:
                connect.update(miss["services"])
            if optional:
                row["path"] = "skipped"
                lo = hi = 0.0
            elif fb_ok:
                row["path"] = "fallback"
                row["fallback"] = _view(fb)
                lo, hi = step_cost(fb.get("cost"), s, q)
                if fb.get("level", "degraded") == "degraded" and status == "full":
                    status = "degraded"
            else:
                row["path"] = "blocked"
                if st.get("blocked_note"):
                    row["blocked_note"] = st["blocked_note"]
                status = "blocked"
                lo = hi = 0.0
        row["cost"] = {"low": round(lo, 2), "high": round(hi, 2)}
        total_lo += lo
        total_hi += hi
        out_steps.append(row)
    kinds = s.get("intake_kinds") or []
    kind = kind if kind in tree()["intake_checklists"] else (kinds[0] if kinds else None)
    result = {
        "scenario": s["id"], "name": s["name"], "status": status, "assume_built": assume_built,
        "services": sorted(present), "quantities": {k: v for k, v in q.items() if v is not None},
        "steps": out_steps,
        "total": {"low": round(total_lo, 2), "high": round(total_hi, 2), "currency": "USD",
                  "note": "marginal cost of outside services per finished video; the Claude subscription is not included"},
        "connect": sorted(connect), "not_built": sorted(later), "record_demand": record_demand,
        "intake": checklist(kind),
    }
    result["summary"] = {"ru": _summary(result, s, "ru"), "en": _summary(result, s, "en")}
    return result


def checklist(kind: str | None) -> dict[str, Any] | None:
    lists = tree()["intake_checklists"]
    if not kind or kind not in lists:
        return None
    return {"kind": kind, "name": lists[kind]["name"],
            "items": {lang: lists[kind]["items"][lang] + lists["_all"]["items"][lang] for lang in ("ru", "en")}}


def _usd(x: float, lang: str) -> str:
    txt = f"{x:.2f}" if x < 10 else f"{x:.0f}"
    return txt.replace(".", ",") if lang == "ru" else txt


def _lower_first(t: str) -> str:
    return t[:1].lower() + t[1:] if t else t


def _used(step_row: dict[str, Any]) -> dict[str, Any]:
    """What actually runs for a plan row: the fallback when it took one, else the step itself."""
    return step_row.get("fallback") or step_row


def _summary(p: dict[str, Any], s: dict[str, Any], lang: str) -> str:
    ru = lang == "ru"
    steps = p["steps"]
    active = [x for x in steps if x["path"] != "skipped"]
    gates = [x for x in active if _used(x)["executor"] == "human_gate"]
    parts = [(f"«{s['name']['ru']}»: {len(active)} шагов — {len(active) - len(gates)} делаю сама, "
              f"{len(gates)} вместе с вами ({'; '.join(_lower_first(_used(g)['title']['ru']) for g in gates)}).")
             if ru else
             (f"\"{s['name']['en']}\": {len(active)} steps — Elsa does {len(active) - len(gates)}, "
              f"{len(gates)} with you ({'; '.join(_used(g)['title']['en'] for g in gates)}).")]
    fb = [x for x in steps if x["path"] == "fallback"]
    if fb:
        joined = "; ".join(f"{_lower_first(x['title'][lang])} → {_lower_first(x['fallback']['title'][lang])}" for x in fb)
        parts.append(("Пока иначе: " if ru else "For now a workaround: ") + joined + ".")
    blocked = [x for x in steps if x["path"] == "blocked"]
    if blocked:
        notes = "; ".join((x.get("blocked_note") or {}).get(lang) or _lower_first(x["title"][lang]) for x in blocked)
        parts.append(("Не получится: " if ru else "Not possible yet: ") + notes + ".")
    skipped = [x for x in steps if x["path"] == "skipped" and x["executor"] != "human_gate"]
    if skipped:
        parts.append(("Можно добавить позже: " if ru else "Can be added later: ")
                     + ", ".join(_lower_first(x["title"][lang]) for x in skipped) + ".")
    if p["connect"]:
        names = ", ".join(groups()[g]["name"][lang] for g in p["connect"])
        parts.append((f"Если подключить {names}, путь станет полнее." if ru
                      else f"Connecting {names} makes the route fuller."))
    t = p["total"]
    mins = p["quantities"].get("minutes", 1)
    mins_txt = f"{mins:g}"
    if t["high"] <= 0:
        parts.append("Сверх подписки Claude — ничего." if ru else "Nothing on top of the Claude subscription.")
    else:
        parts.append((f"Деньги: примерно ${_usd(t['low'], lang)}–{_usd(t['high'], lang)} за видео около {mins_txt} мин, "
                      f"сверх подписки Claude." if ru else
                      f"Money: about ${_usd(t['low'], lang)}–{_usd(t['high'], lang)} per video of about {mins_txt} min, "
                      f"on top of the Claude subscription."))
    return " ".join(parts)


# ---- detection ----------------------------------------------------------------------------------

def _kind_of(p: Path) -> str | None:
    ext = p.suffix.lower()
    for kind, exts in (("video", VIDEO_EXT), ("subs", SUB_EXT), ("audio", AUDIO_EXT), ("image", IMAGE_EXT),
                       ("text", TEXT_EXT)):
        if ext in exts:
            return kind
    if ext == ".json" and p.stem.endswith("_cut"):
        return "words"
    return None


def inventory(path: str | Path) -> dict[str, Any]:
    """What the person brought: files by kind (names relative to the folder), readable text."""
    root = Path(path).expanduser()
    if not root.exists():
        raise VPError("data", f"{root} does not exist")
    inv: dict[str, Any] = {"video": [], "subs": [], "audio": [], "image": [], "text": [], "words": [],
                           "request_kind": None, "text_chars": 0, "text_sample": ""}
    files: list[Path] = []
    if root.is_file():
        files = [root]
        base = root.parent
    else:
        base = root
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                                 and depth < MAX_DEPTH)
            for f in sorted(filenames):
                if not f.startswith(".") and f not in SKIP_FILES:
                    files.append(Path(dirpath) / f)
            if len(files) >= MAX_FILES:
                break
        req = root / "request.yaml"
        if req.is_file():
            from .core.io import read_yaml
            data = read_yaml(req, default={}) or {}
            inv["request_kind"] = str((data.get("kind") if isinstance(data, dict) else None) or "recording").lower()
    texts: list[str] = []
    for f in files[:MAX_FILES]:
        k = _kind_of(f)
        if not k:
            continue
        inv[k].append(str(f.relative_to(base)))
        if k == "text" and f.suffix.lower() in READABLE_TEXT:
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace")[:20000])
            except OSError:
                pass
    joined = "\n".join(texts)
    inv["text_chars"] = len(joined.strip())
    inv["text_sample"] = joined[:20000]
    inv["base"] = str(base)
    return inv


def _default_probe(p: Path) -> bool | None:
    try:
        from .adapters import media
        if not media.available():
            return None
        return bool(media.probe(p)["has_audio"])
    except Exception:  # noqa: BLE001 — a fake or broken file says nothing
        return None


def _keywords_hit(words: Iterable[str], text: str) -> list[str]:
    low = text.lower()
    return [w for w in words if w.lower() in low]


def detect(path: str | Path, says: str | None = None,
           probe: Callable[[Path], bool | None] | None = _default_probe) -> dict[str, Any]:
    inv = inventory(path)
    scores: dict[str, int] = {s["id"]: 0 for s in scenario_list()}
    reasons: dict[str, list[str]] = {k: [] for k in scores}

    def add(sid: str, pts: int, why: str) -> None:
        scores[sid] += pts
        reasons[sid].append(why)

    v, sub, au, im, tx = (len(inv[k]) for k in ("video", "subs", "audio", "image", "text"))
    audio_flags: list[bool] = []
    if probe and v:
        for name in inv["video"][:MAX_PROBES]:
            r = probe(Path(inv["base"]) / name)
            if r is not None:
                audio_flags.append(r)
    silent = bool(audio_flags) and not any(audio_flags)
    speech = any(audio_flags)

    if inv["request_kind"] == "text":
        add("B", 100, "request.yaml of a text job")
    elif inv["request_kind"]:
        add("D", 80, "request.yaml of a recording job")
    if v and sub:
        add("D", 60, f"{v} video(s) with {sub} subtitle file(s)")
        add("K", 30, "a video with subtitles can also be a finished film to repack")
    elif v:
        if silent:
            add("G", 70, "the videos have no sound track")
            if im or tx:
                add("H", 20, "pictures or texts next to the footage")
        elif v == 1:
            add("E", 60, "one video, no subtitles")
            add("K", 25, "one video can also be a finished film to repack")
            add("F", 15, "one video can be a talking-head take")
            if speech:
                add("E", 10, "the video has a sound track")
        else:
            add("G", 45 if v >= 3 else 25, f"{v} videos, no subtitles")
            add("F", 40, f"{v} videos can be talking-head takes")
            add("E", 15, "videos without subtitles")
            if speech:
                add("F", 15, "the videos have a sound track")
                add("E", 10, "the videos have a sound track")
        if (im or tx) and not silent:
            add("H", 35, "footage together with pictures or texts")
    else:
        if au:
            add("J", 60, f"{au} audio file(s), no video")
        if im:
            add("I", 60, f"{im} picture(s), no video")
        if tx and not au and not im:
            if SCRIPT_MARKERS.search(inv["text_sample"]):
                add("C", 65, "the text has script markers (narrator, scenes)")
                add("B", 30, "a text")
            elif inv["text_chars"] < IDEA_MAX_CHARS:
                add("A", 55, f"a short text ({inv['text_chars']} characters) — likely an idea")
                add("B", 20, "a text")
            else:
                add("B", 60, f"a text of {inv['text_chars']} characters")
                add("C", 15, "a text can be a ready script")
    if not any((v, sub, au, im, tx, inv["request_kind"])):
        add("A", 30, "no materials yet")
    for s in scenario_list():
        kws = s["detection"]["keywords"]
        if says:
            hit = _keywords_hit(kws, says)
            if hit:
                add(s["id"], SAYS_KEYWORD, f"the person said: {', '.join(hit[:3])}")
        if inv["text_sample"]:
            hit = _keywords_hit(kws, inv["text_sample"])
            if hit:
                add(s["id"], TEXT_KEYWORD, f"the text mentions: {', '.join(hit[:3])}")

    order = {s["id"]: i for i, s in enumerate(scenario_list())}
    ranked = sorted((sid for sid in scores if scores[sid] > 0), key=lambda x: (-scores[x], order[x]))
    cands = [{"scenario": sid, "name": scenario(sid)["name"], "score": scores[sid], "reasons": reasons[sid]}
             for sid in ranked[:4]]
    ambiguous = not cands or cands[0]["score"] < WEAK_SCORE or (
        len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < AMBIGUOUS_MARGIN)
    question = None
    if ambiguous:
        if len(cands) >= 2:
            pair = [c["scenario"] for c in cands[:2]]
        elif cands:
            pair = [cands[0]["scenario"], "A" if cands[0]["scenario"] != "A" else "B"]
        else:
            pair = ["A", "B"]
        question = {"ru": "Что ближе к вашей задаче?", "en": "Which is closer to what you need?",
                    "options": [{"scenario": sid, "ru": scenario(sid)["detection"]["choice"]["ru"],
                                 "en": scenario(sid)["detection"]["choice"]["en"]} for sid in pair]}
    top = cands[0]["scenario"] if cands else None
    kind = _guess_kind(" ".join(filter(None, (says, inv["text_sample"]))))
    if not kind and top:
        kind = (scenario(top).get("intake_kinds") or [None])[0]
    counts = {k: len(inv[k]) for k in ("video", "subs", "audio", "image", "text", "words")}
    return {"path": str(Path(path).expanduser()), "inventory": counts,
            "audio_probe": {"probed": len(audio_flags), "with_sound": sum(audio_flags)},
            "top": top, "candidates": cands, "ambiguous": ambiguous, "question": question,
            "intake_kind": kind}


def _guess_kind(text: str) -> str | None:
    if not text:
        return None
    best, best_n = None, 0
    for kind, words in tree()["intake_kind_keywords"].items():
        n = len(_keywords_hit(words, text))
        if n > best_n:
            best, best_n = kind, n
    return best


# ---- matrix -------------------------------------------------------------------------------------

def _set_groups(spec: tuple[str, ...]) -> set[str]:
    if "*" in spec:
        return set(groups())
    return assumed_groups() | set(spec)


def matrix() -> dict[str, Any]:
    rows = []
    for s in scenario_list():
        row: dict[str, Any] = {"scenario": s["id"], "name": s["name"], "doc_status": s.get("doc_status"),
                               "sets": {}}
        for set_name, spec in MATRIX_SETS:
            present = _set_groups(spec)
            cell: dict[str, Any] = {}
            for view, ab in (("today", False), ("designed", True)):
                p1 = plan(s["id"], present=present, assume_built=ab)
                missing_nb: list[str] = []
                missing_sv: list[str] = []
                for st in p1["steps"]:
                    if st["path"] in ("fallback", "blocked") and not st["optional"]:
                        for c in st["missing"]["not_built"]:
                            if c not in missing_nb:
                                missing_nb.append(c)
                        for g in st["missing"]["services"]:
                            if g not in missing_sv:
                                missing_sv.append(g)
                cost = {}
                for m in MATRIX_MINUTES:
                    t = plan(s["id"], minutes=m, present=present, assume_built=ab)["total"]
                    cost[str(m)] = [t["low"], t["high"]]
                cell[view] = {"achievable": p1["status"], "missing_not_built": missing_nb,
                              "missing_services": missing_sv, "cost_usd": cost}
            row["sets"][set_name] = cell
        rows.append(row)
    return {"sets": [{"name": n, "groups": sorted(_set_groups(sp))} for n, sp in MATRIX_SETS],
            "minutes": list(MATRIX_MINUTES), "rows": rows,
            "views": {"today": "planned capabilities are unavailable — what a person can count on now",
                      "designed": "planned capabilities count as built when their service is connected — what the services unlock once the adapters land"}}


def _code(cap: str) -> str:
    c = capabilities()[cap]["code"]
    return c if c and c != "—" else cap


def _cell(c: dict[str, Any]) -> str:
    miss = [_code(x) for x in c["missing_not_built"]] + [f"+{g}" for g in c["missing_services"]]
    return c["achievable"] + (f" ({', '.join(miss)})" if miss else "")


def _cost_cell(c: dict[str, Any]) -> str:
    def r(pair: list[float]) -> str:
        lo, hi = pair
        if hi <= 0:
            return "0"
        f = (lambda x: f"{x:.2f}" if x < 10 else f"{x:.0f}")
        return f(lo) if lo == hi else f"{f(lo)}–{f(hi)}"
    return " / ".join(r(c["cost_usd"][str(m)]) for m in MATRIX_MINUTES)


def matrix_markdown(m: dict[str, Any] | None = None) -> str:
    m = m or matrix()
    names = [s["name"] for s in m["sets"]]
    lines = [
        "# Scenario matrix — what a person can count on, by connected services",
        "",
        f"> Generated by `{REGEN}` from `src/vp/knowledge/scenarios.json` and",
        "> `src/vp/knowledge/capabilities.json`. Do not edit by hand: change the data and regenerate",
        "> (a test compares this file with the data).",
        "",
        "Every set includes the free pieces: Claude itself, the animation pack (Remotion) and the YouTube",
        "connection. `bare` = Claude only. `all` = every service group, including stock, avatar and other",
        "platforms. Cell = how the scenario goes: **full** (main path), **degraded** (a workaround on at least",
        "one required step), **blocked** (a required step has no workaround). In brackets: what is missing —",
        "a capability code (docs/SCENARIOS.md §2) that is not built yet, or `+service` not connected.",
        "",
        "Cost = marginal USD for outside services per finished video of 1 / 5 / 10 minutes, Claude subscription",
        "not included; constants and their sources in `scenarios.json` → `constants`",
        "(docs/research/service-costs-2026-11-20.md).",
        "",
    ]
    for view, title in (("today", "Today (planned capabilities unavailable)"),
                        ("designed", "Designed (planned capabilities counted as built when their service is connected)")):
        lines += [f"## {title}", "", "| Scenario | " + " | ".join(names) + " |",
                  "|---|" + "---|" * len(names)]
        for row in m["rows"]:
            cells = [_cell(row["sets"][n][view]) for n in names]
            lines.append(f"| {row['scenario']} {row['name']['en']} | " + " | ".join(cells) + " |")
        lines.append("")
        lines += [f"### Cost, USD, {view} — 1 / 5 / 10 min", "", "| Scenario | " + " | ".join(names) + " |",
                  "|---|" + "---|" * len(names)]
        for row in m["rows"]:
            cells = [_cost_cell(row["sets"][n][view]) for n in names]
            lines.append(f"| {row['scenario']} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


# ---- validation ---------------------------------------------------------------------------------

def _cli_commands() -> set[str]:
    from . import cli
    names = set(cli.SUBCOMMAND_MAINS)
    for a in cli.build_parser()._subparsers._group_actions:  # noqa: SLF001 — argparse has no public API
        names |= set(getattr(a, "choices", {}) or {})
    return names


def validate() -> list[str]:
    problems: list[str] = []
    caps = capabilities()
    gr = groups()
    consts = tree()["constants"]
    qtys = tree()["quantities"]
    lists = tree()["intake_checklists"]
    cli_cmds = _cli_commands()
    for cid, c in caps.items():
        if c.get("group") not in gr:
            problems.append(f"capability {cid}: unknown group {c.get('group')!r}")
        if c.get("status") not in STATUSES:
            problems.append(f"capability {cid}: bad status {c.get('status')!r}")
    for name, c in consts.items():
        if not c.get("source") or "low" not in c or "high" not in c or c["low"] > c["high"]:
            problems.append(f"constant {name}: needs low <= high and a source")

    def check_cmd(where: str, cmd: Any, needs: list[str]) -> None:
        if cmd is None:
            return
        if not isinstance(cmd, str):
            problems.append(f"{where}: command must be a string or null")
        elif cmd.startswith("vp "):
            sub = cmd.split()[1]
            if sub not in cli_cmds:
                problems.append(f"{where}: `{cmd}` — no such vp command")
            planned = [n for n in needs if n in caps and caps[n]["status"] not in AVAILABLE_STATUSES]
            if planned:
                problems.append(f"{where}: built command `{cmd}` but needs planned {planned}")
        elif cmd.startswith("planned:"):
            cap = cmd.split(":", 1)[1]
            if cap not in caps:
                problems.append(f"{where}: planned marker for unknown capability {cap!r}")
            elif caps[cap]["status"] in AVAILABLE_STATUSES:
                problems.append(f"{where}: capability {cap} is {caps[cap]['status']} — replace `{cmd}` with the real command")
        elif not cmd.startswith("mcp:"):
            problems.append(f"{where}: command {cmd!r} must start with `vp `, `planned:` or `mcp:`")

    def check_exec(where: str, ex: Any) -> None:
        if isinstance(ex, str) and ex.startswith("service:"):
            if ex.split(":", 1)[1] not in caps:
                problems.append(f"{where}: executor names unknown capability {ex!r}")
        elif ex not in EXECUTORS:
            problems.append(f"{where}: bad executor {ex!r}")

    for s in scenario_list():
        sid = s["id"]
        det = s.get("detection") or {}
        if not det.get("keywords") or not (det.get("choice") or {}).get("ru") or not det.get("signals"):
            problems.append(f"scenario {sid}: detection needs signals, keywords and a choice")
        for k in s.get("intake_kinds") or []:
            if k not in lists:
                problems.append(f"scenario {sid}: unknown intake kind {k!r}")
        if not s.get("intake_kinds"):
            problems.append(f"scenario {sid}: no intake kind")
        seen: set[str] = set()
        try:
            steps = steps_of(s)
        except VPError as e:
            problems.append(f"scenario {sid}: {e}")
            continue
        for st in steps:
            where = f"scenario {sid} step {st.get('id')}"
            if st.get("id") in seen:
                problems.append(f"{where}: duplicate step id")
            seen.add(st.get("id"))
            if not (st.get("title") or {}).get("ru") or not (st.get("title") or {}).get("en"):
                problems.append(f"{where}: title needs ru and en")
            needs = st.get("needs") or []
            for n in needs:
                if n not in caps:
                    problems.append(f"{where}: unknown capability {n!r}")
            check_exec(where, st.get("executor"))
            check_cmd(where, st.get("command"), needs)
            cost = st.get("cost")
            if cost:
                if cost.get("qty") not in qtys:
                    problems.append(f"{where}: unknown cost quantity {cost.get('qty')!r}")
                for f in cost.get("factors") or []:
                    if f not in consts:
                        problems.append(f"{where}: unknown cost constant {f!r}")
            fb = st.get("fallback")
            if fb:
                for n in fb.get("needs") or []:
                    if n not in caps:
                        problems.append(f"{where} fallback: unknown capability {n!r}")
                check_exec(f"{where} fallback", fb.get("executor"))
                check_cmd(f"{where} fallback", fb.get("command"), fb.get("needs") or [])
                if fb.get("level", "degraded") not in ("degraded", "full"):
                    problems.append(f"{where} fallback: bad level {fb.get('level')!r}")
            can_be_missing = any(caps[n]["status"] not in AVAILABLE_STATUSES
                                 or not gr[caps[n]["group"]].get("assumed") for n in needs if n in caps)
            if can_be_missing and not st.get("optional") and not fb and not st.get("blocked_note"):
                problems.append(f"{where}: can be unavailable but has no fallback and no blocked_note")
    return problems


# ---- CLI ----------------------------------------------------------------------------------------

def _print(obj: Any, as_json: bool, text: Callable[[Any], str]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json else text(obj))


def _plan_text(p: dict[str, Any]) -> str:
    lines = [f"{p['scenario']} {p['name']['en']} — {p['status']}; ${p['total']['low']}–{p['total']['high']}"]
    for st in p["steps"]:
        used = _used(st)
        cmd = used.get("command") or "(dialog)"
        lines.append(f"  {st['path']:<8} {st['id']:<15} {used['executor']:<24} {cmd}  "
                     f"${st['cost']['low']}–{st['cost']['high']}")
    lines.append(p["summary"]["en"])
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp scenario", description="scenario tree: route, steps, services, cost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("list")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("detect")
    a.add_argument("path")
    a.add_argument("--says")
    a.add_argument("--no-probe", action="store_true")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("plan")
    a.add_argument("id")
    a.add_argument("--minutes", type=float, default=1)
    a.add_argument("--source-hours", type=float)
    a.add_argument("--images", type=int)
    a.add_argument("--ai-video-seconds", type=float)
    a.add_argument("--services", default="auto")
    a.add_argument("--assume-built", action="store_true")
    a.add_argument("--kind")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("matrix")
    a.add_argument("--json", action="store_true")
    a.add_argument("--write")
    a = sub.add_parser("services")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("check")
    a.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            data = [{"id": s["id"], "name": s["name"], "doc_status": s.get("doc_status"), "input": s["input"],
                     "intake_kinds": s.get("intake_kinds"), "steps": len(s["steps"])} for s in scenario_list()]
            _print(data, args.json, lambda d: "\n".join(f"{x['id']}  {x['name']['en']}  ({x['doc_status']})" for x in d))
            return 0
        if args.cmd == "detect":
            d = detect(args.path, says=args.says, probe=None if args.no_probe else _default_probe)
            _print(d, args.json, lambda r: "\n".join(
                [f"{c['scenario']} {c['score']:>3}  {c['name']['en']}: {'; '.join(c['reasons'])}" for c in r["candidates"]]
                + ([f"ask: {r['question']['en']} " + " | ".join(o["en"] for o in r["question"]["options"])]
                   if r["question"] else [])))
            return 0
        if args.cmd == "plan":
            present = parse_services(args.services)
            p = plan(args.id, minutes=args.minutes, source_hours=args.source_hours, images=args.images,
                     ai_video_seconds=args.ai_video_seconds, present=present, assume_built=args.assume_built,
                     kind=args.kind)
            p["services_detected"] = present
            _print(p, args.json, _plan_text)
            return 0
        if args.cmd == "matrix":
            m = matrix()
            if args.write:
                Path(args.write).write_text(matrix_markdown(m), encoding="utf-8")
            if args.json or not args.write:
                _print(m, args.json, matrix_markdown)
            else:
                print(f"written {args.write}")
            return 0
        if args.cmd == "services":
            found = detect_groups()
            _print(found, args.json, lambda f: "\n".join(f"{g}: {why}" for g, why in sorted(f.items())))
            return 0
        if args.cmd == "check":
            probs = validate()
            _print({"ok": not probs, "problems": probs}, args.json,
                   lambda r: "OK scenario data" if r["ok"] else "\n".join("FAIL " + x for x in r["problems"]))
            if probs:
                note_failure(None, "validation")
            return 0 if not probs else 2
    except VPError as e:
        note_failure(e)
        print(f"ERROR {e}")
        return 2
    return 3  # pragma: no cover
