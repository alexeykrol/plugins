"""Tutorials at the moment of need (learning track U3, docs/tutorials/plan-2026-11-23.md §4.1).

Data, not prose: `vp/knowledge/tutorials.json` — the closed list T01–T08 with titles, what the
person can do after, the moments each belongs to, the duration, the video `url` (empty until the
video is produced) and a short teaching card for the chat. This module only does the bookkeeping
(SPEC §0, llm-as-component): which tutorial belongs to a moment, whether its conditions hold,
whether the person already answered the offer. How to say it, and whether the moment is right at
all, stays with the director in the dialog.

A moment is either a step of a route — `--route R --step S`, both from the closed vocabulary
vp.telemetry.ROUTE_STEPS, the tutorial is offered BEFORE that step — or one of MOMENTS, the
moments outside the routes (`--moment M`).

Conditions (`only_if`, closed list CONDITIONS) are facts read by code, never the model's memory:
  first_project       no registered project holds a finished video yet (vp.projects.summary)
  no_elevenlabs_key   no ElevenLabs key is saved (vp.keys; the key itself is never read out)

State: <VP_HOME>/learning.json, on the person's computer only — never in telemetry:
  {"tutorials": {"T04": {"answer": "shown" | "declined", "at": "<ISO time>"}}}
A tutorial is OFFERED at most once per computer: once the person watched it, heard the card or
declined, `for` no longer puts it into `offer` (it stays in `tutorials` with `answered`, and is
always there when the person asks for it by name: `vp tutorial list`).

`for` returns at most ONE offer per moment (the first tutorial of the data order that applies and
is not answered): one question per message.

A tutorial with an empty `url` is `available: false`: there is no video to show yet — the director
says the `card` in the chat instead (the same content, 2–4 sentences).

Offers of a job (closed list JOB_OFFERS) are things the director proposes ONCE per project — e.g.
a CapCut project after the ready videos of a recording. Their state lives in the job folder, next
to run.json: <job>/learning.json {"offers": {"capcut_project": {"answer": "accepted" | "declined",
"at": "<ISO time>"}}}. Never in telemetry.

CLI:
  vp tutorial list [--lang ru|en] [--json]
  vp tutorial for (--route R --step S | --moment M) [--lang ru|en] [--json]
  vp tutorial set <id> --answer shown|declined [--json]     record the person's answer
  vp tutorial offer <job> <offer> [--answer accepted|declined] [--json]
                                                            a once-per-job offer: asked already?
  vp tutorial check [--json]                                data validity (exit 2 on problems)
"""
from __future__ import annotations

import argparse
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .core.errors import VPError, note_failure

FILE = Path(__file__).resolve().parent / "knowledge" / "tutorials.json"
STATE_FILE = "learning.json"
LANGS = ("ru", "en")
ANSWERS = ("shown", "declined")
MOMENTS = ("install", "first_project", "stuck", "elevenlabs_key", "long_work")
NEVER_OFFERED = ("install",)          # Elsa is not installed yet at that moment
JOB_OFFERS = ("capcut_project",)      # proposed by the director once per job
JOB_ANSWERS = ("accepted", "declined")
LABELS = ("video", "card", "skip")
ID_RE = re.compile(r"T\d\d")
CARD_MAX_CHARS = 700                  # 2–4 sentences
CARD_SENTENCES = (2, 4)
SCRIPT_RE = re.compile(r"docs/tutorials/[a-z0-9-]+\.md")


# ---- conditions: facts read by code -------------------------------------------------------------

def _first_project() -> bool:
    from .projects import summary
    return bool(summary()["first_project"])


def _no_elevenlabs_key() -> bool:
    from .keys import get_key
    return not get_key("elevenlabs")


CONDITIONS: dict[str, Callable[[], bool]] = {
    "first_project": _first_project,
    "no_elevenlabs_key": _no_elevenlabs_key,
}


# ---- data ---------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def data() -> dict[str, Any]:
    return json.loads(FILE.read_text(encoding="utf-8"))


def tutorials() -> list[dict[str, Any]]:
    return data()["tutorials"]


def tutorial(tid: str) -> dict[str, Any]:
    want = str(tid).strip().upper()
    for t in tutorials():
        if t["id"] == want:
            return t
    raise VPError("data", f"unknown tutorial {tid!r} (known: {', '.join(t['id'] for t in tutorials())})")


def _sentences(text: str) -> int:
    return len(re.findall(r"[.!?…](?:[»\"”)]*)(?:\s|$)", text))


def _two_langs(obj: Any) -> bool:
    return isinstance(obj, dict) and all(isinstance(obj.get(k), str) and obj[k].strip() for k in LANGS)


def problems(d: dict[str, Any] | None = None) -> list[str]:
    """Everything wrong with the data (empty list = valid)."""
    from . import telemetry
    d = data() if d is None else d
    out: list[str] = []
    labels = d.get("labels") if isinstance(d.get("labels"), dict) else {}
    for name in LABELS:
        if not _two_langs(labels.get(name)):
            out.append(f"labels.{name}: ru and en are required")
    for lang in LANGS:
        if "{duration}" not in str((labels.get("video") or {}).get(lang, "")):
            out.append(f"labels.video.{lang}: must contain {{duration}}")
    if set(d.get("moments") or {}) != set(MOMENTS):
        out.append(f"moments: must describe exactly {', '.join(MOMENTS)}")
    if set(d.get("conditions") or {}) != set(CONDITIONS):
        out.append(f"conditions: must describe exactly {', '.join(sorted(CONDITIONS))}")
    seen: set[str] = set()
    items = d.get("tutorials")
    if not isinstance(items, list) or not items:
        return out + ["tutorials: a non-empty list is required"]
    for n, t in enumerate(items):
        if not isinstance(t, dict):
            out.append(f"tutorials[{n}]: not an object")
            continue
        tid = t.get("id")
        where = str(tid or f"tutorials[{n}]")
        if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
            out.append(f"{where}: id must look like T01")
        elif tid in seen:
            out.append(f"{tid}: duplicate id")
        seen.add(str(tid))
        for key in ("title", "after", "card"):
            if not _two_langs(t.get(key)):
                out.append(f"{where}: {key}.ru and {key}.en are required")
        if _two_langs(t.get("card")):
            for lang in LANGS:
                card = t["card"][lang]
                count = _sentences(card)
                if len(card) > CARD_MAX_CHARS or not CARD_SENTENCES[0] <= count <= CARD_SENTENCES[1]:
                    out.append(f"{where}: card.{lang} must be {CARD_SENTENCES[0]}–{CARD_SENTENCES[1]} "
                               f"sentences and at most {CARD_MAX_CHARS} characters "
                               f"(has {count} and {len(card)})")
        dur = t.get("duration_s")
        if not isinstance(dur, int) or isinstance(dur, bool) or not 10 <= dur <= 600:
            out.append(f"{where}: duration_s must be whole seconds, 10..600")
        url = t.get("url")
        if not isinstance(url, str) or (url and not re.fullmatch(r"https://[^\s\"'<>]+", url)):
            out.append(f"{where}: url must be https://… or empty")
        if not isinstance(t.get("script"), str) or not SCRIPT_RE.fullmatch(t["script"]):
            out.append(f"{where}: script must be a brief under docs/tutorials/")
        when, moments = t.get("when"), t.get("moments")
        if not isinstance(when, list) or not isinstance(moments, list):
            out.append(f"{where}: when and moments must be lists")
            continue
        if not when and not moments:
            out.append(f"{where}: belongs to no moment (when and moments are both empty)")
        for w in when:
            route, step = (w.get("route"), w.get("step")) if isinstance(w, dict) else (None, None)
            if route not in telemetry.ROUTE_STEPS:
                out.append(f"{where}: unknown route {route!r}")
            elif step not in telemetry.ROUTE_STEPS[route]:
                out.append(f"{where}: step {step!r} is not a step of the {route} route")
        for m in moments:
            if m not in MOMENTS:
                out.append(f"{where}: unknown moment {m!r}")
        conds = t.get("only_if")
        if not isinstance(conds, list):
            out.append(f"{where}: only_if must be a list")
        else:
            for c in conds:
                if c not in CONDITIONS:
                    out.append(f"{where}: unknown condition {c!r}")
    return out


# ---- state: the person's answers, on their computer -----------------------------------------------

def state_path(home: Path | None = None) -> Path:
    from .core.paths import vp_home
    return (home or vp_home()) / STATE_FILE


def answers(home: Path | None = None) -> dict[str, dict[str, Any]]:
    from .core.io import read_json
    try:
        got = read_json(state_path(home), default=None)
    except (ValueError, OSError):
        got = None
    items = got.get("tutorials") if isinstance(got, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items()
            if isinstance(v, dict) and v.get("answer") in ANSWERS}


def set_answer(tid: str, answer: str, home: Path | None = None, now: str | None = None) -> dict[str, Any]:
    """Record that the person watched / heard the tutorial (`shown`) or passed (`declined`).
    A later `shown` replaces `declined` (they asked for it after all); never the other way."""
    from .core.io import now_iso, read_json, write_json
    t = tutorial(tid)
    if answer not in ANSWERS:
        raise VPError("data", f"answer must be one of: {', '.join(ANSWERS)}")
    try:
        got = read_json(state_path(home), default=None)
    except (ValueError, OSError):
        got = None
    state = got if isinstance(got, dict) else {}
    items = state.get("tutorials") if isinstance(state.get("tutorials"), dict) else {}
    old = items.get(t["id"]) if isinstance(items.get(t["id"]), dict) else None
    if old and old.get("answer") == "shown" and answer == "declined":
        return {"id": t["id"], **old, "kept": True}
    rec = {"answer": answer, "at": now or now_iso()}
    items[t["id"]] = rec
    state["tutorials"] = items
    write_json(state_path(home), state)
    return {"id": t["id"], **rec}


# ---- once-per-job offers: state in the job folder ---------------------------------------------------

def _job_folder(job: str | Path) -> Path:
    folder = Path(job).expanduser()
    if not folder.is_dir():
        raise VPError("data", f"job folder not found: {folder}")
    return folder


def _job_offers(folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from .core.io import read_json
    try:
        got = read_json(folder / STATE_FILE, default=None)
    except (ValueError, OSError):
        got = None
    state = got if isinstance(got, dict) else {}
    offers = state.get("offers") if isinstance(state.get("offers"), dict) else {}
    return state, offers


def job_offer(job: str | Path, offer: str, answer: str | None = None,
              now: str | None = None) -> dict[str, Any]:
    """Was this offer already answered in this job? With `answer` — record it (the first answer
    stays: an offer is made once, a later call never rewrites it)."""
    from .core.io import now_iso, write_json
    if offer not in JOB_OFFERS:
        raise VPError("data", f"unknown offer {offer!r} (known: {', '.join(JOB_OFFERS)})")
    if answer is not None and answer not in JOB_ANSWERS:
        raise VPError("data", f"answer must be one of: {', '.join(JOB_ANSWERS)}")
    folder = _job_folder(job)
    state, offers = _job_offers(folder)
    old = offers.get(offer) if isinstance(offers.get(offer), dict) else None
    if old and old.get("answer") not in JOB_ANSWERS:
        old = None
    if answer is not None and old is None:
        old = {"answer": answer, "at": now or now_iso()}
        offers[offer] = old
        state["offers"] = offers
        write_json(folder / STATE_FILE, state)
    return {"job": str(folder), "offer": offer, "answered": old["answer"] if old else None,
            "at": old["at"] if old else None}


# ---- the question of the dialog: what belongs to this moment ---------------------------------------

def duration_label(seconds: int, lang: str) -> str:
    """90 -> «1,5 мин» / "1.5 min": half minutes, never less than half."""
    halves = max(1, math.floor(seconds / 30 + 0.5))
    whole, half = divmod(halves, 2)
    num = f"{whole}{',5' if lang == 'ru' else '.5'}" if half else str(whole)
    return f"{num} {'мин' if lang == 'ru' else 'min'}"


def _pick(obj: dict[str, str], lang: str) -> str:
    return obj.get(lang) or obj["en"]


def view(t: dict[str, Any], lang: str, answered: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """One tutorial as the dialog needs it, in one language."""
    got = (answers() if answered is None else answered).get(t["id"])
    return {"id": t["id"], "title": _pick(t["title"], lang), "after": _pick(t["after"], lang),
            "duration_s": t["duration_s"], "duration": duration_label(t["duration_s"], lang),
            "available": bool(t["url"]), "url": t["url"], "card": _pick(t["card"], lang),
            "when": t["when"], "moments": t["moments"], "only_if": t["only_if"],
            "answered": got["answer"] if got else None}


def _holds(cond: str) -> bool:
    """A condition that cannot be read does not hold: no offer rather than a wrong one."""
    try:
        return bool(CONDITIONS[cond]())
    except Exception:  # noqa: BLE001
        return False


def for_moment(route: str | None = None, step: str | None = None, moment: str | None = None,
               lang: str = "ru") -> dict[str, Any]:
    """The tutorials of a moment and the ONE to offer now (or None).

    {"route", "step", "moment", "tutorials": [view + "applies"], "offer": None | {"id", "title",
     "available", "url", "card", "show_label", "skip_label", "duration"}}"""
    from . import telemetry
    if moment is not None:
        if moment not in MOMENTS:
            raise VPError("data", f"unknown moment {moment!r} (known: {', '.join(MOMENTS)})")
    elif route not in telemetry.ROUTE_STEPS:
        raise VPError("data", f"unknown route {route!r} (known: {', '.join(telemetry.ROUTE_STEPS)})")
    elif step not in telemetry.ROUTE_STEPS[route]:
        raise VPError("data", f"{step!r} is not a step of the {route} route "
                              f"(known: {', '.join(telemetry.ROUTE_STEPS[route])})")
    if lang not in LANGS:
        lang = "en"
    answered = answers()
    labels = data()["labels"]
    rows: list[dict[str, Any]] = []
    offer: dict[str, Any] | None = None
    for t in tutorials():
        if moment is not None:
            if moment not in t["moments"]:
                continue
        elif {"route": route, "step": step} not in t["when"]:
            continue
        row = view(t, lang, answered)
        row["applies"] = all(_holds(c) for c in t["only_if"])
        rows.append(row)
        if offer is None and row["applies"] and row["answered"] is None \
                and moment not in NEVER_OFFERED:
            show = _pick(labels["video" if row["available"] else "card"], lang)
            offer = {"id": row["id"], "title": row["title"], "available": row["available"],
                     "url": row["url"], "card": row["card"], "duration": row["duration"],
                     "show_label": show.replace("{duration}", row["duration"]),
                     "skip_label": _pick(labels["skip"], lang)}
    return {"route": route if moment is None else None, "step": step if moment is None else None,
            "moment": moment, "tutorials": rows, "offer": offer}


# ---- CLI ------------------------------------------------------------------------------------------

def _print(obj: Any, as_json: bool, text: Callable[[Any], str]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1) if as_json else text(obj))


def _moments_text(row: dict[str, Any]) -> str:
    return ", ".join([f"{w['route']}/{w['step']}" for w in row["when"]] + list(row["moments"])) or "-"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vp tutorial",
                                 description="tutorials at the moment of need (data: knowledge/tutorials.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="every tutorial: title, moments, video or card, the answer so far")
    fo = sub.add_parser("for", help="the tutorials of a moment and the one to offer now")
    fo.add_argument("--route", help="a route of the step vocabulary: recording | text")
    fo.add_argument("--step", help="the step BEFORE which the tutorial is offered")
    fo.add_argument("--moment", help="a moment outside the routes: " + " | ".join(MOMENTS))
    for p in (ls, fo):
        p.add_argument("--lang", default="ru", help="ru | en (default ru)")
    st = sub.add_parser("set", help="record the person's answer to the offer")
    st.add_argument("id")
    st.add_argument("--answer", required=True, choices=ANSWERS)
    of = sub.add_parser("offer", help="a once-per-job offer: was it asked, record the answer")
    of.add_argument("job")
    of.add_argument("offer", choices=JOB_OFFERS)
    of.add_argument("--answer", choices=JOB_ANSWERS)
    ck = sub.add_parser("check", help="data validity")
    for p in (ls, fo, st, of, ck):
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        bad = problems()
        _print({"ok": not bad, "problems": bad}, a.json,
               lambda d: "OK" if d["ok"] else "\n".join(f"PROBLEM {p}" for p in d["problems"]))
        if bad:
            note_failure(None, "data")
        return 2 if bad else 0
    if a.cmd == "list":
        lang = a.lang if a.lang in LANGS else "en"
        rows = [view(t, lang) for t in tutorials()]
        _print(rows, a.json, lambda d: "\n".join(
            f"{r['id']}\t{'video' if r['available'] else 'card'}\t{r['duration']}\t"
            f"{r['answered'] or '-'}\t{_moments_text(r)}\t{r['title']}" for r in d))
        return 0
    if a.cmd == "offer":
        res = job_offer(a.job, a.offer, a.answer)
        _print(res, a.json, lambda d: f"{d['offer']}: {d['answered'] or 'not asked yet'}")
        return 0
    if a.cmd == "set":
        res = set_answer(a.id, a.answer)
        _print(res, a.json, lambda d: f"RECORDED {d['id']}: {d['answer']}"
               + (" (kept: already shown)" if d.get("kept") else ""))
        return 0
    if bool(a.moment) == bool(a.route or a.step) or (not a.moment and not (a.route and a.step)):
        ap.error("give either --route with --step, or --moment")
    res = for_moment(a.route, a.step, a.moment, a.lang)

    def text(d: dict[str, Any]) -> str:
        o = d["offer"]
        lines = [f"{r['id']}\t{'applies' if r['applies'] else 'not now'}\t{r['answered'] or '-'}\t"
                 f"{r['title']}" for r in d["tutorials"]] or ["no tutorial belongs to this moment"]
        lines.append("OFFER none" if o is None else
                     f"OFFER {o['id']} ({'video ' + o['url'] if o['available'] else 'card, no video yet'}): "
                     f"«{o['show_label']}» / «{o['skip_label']}»")
        return "\n".join(lines)

    _print(res, a.json, text)
    return 0
