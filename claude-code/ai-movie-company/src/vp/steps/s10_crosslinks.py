"""S10 — cross-links across the series (SPEC S10). Job scope, after every S8 instance, before S9.

Judgment (task `s10.graph`): the model gets the items as numbered texts — type, order in the
recording, title, thesis — and returns links as item NUMBERS with a kind
(fragment | next | prev | related). It never sees video ids, URLs or times.

Bookkeeping (code): every fragment gets an automatic link to the full recording at its place
(t = fragment start in the recording + intro_ms of the full render manifest); the model's links
are validated (numbers exist, no self/duplicate links, no links to the full recording, ≤ 4 per
fragment, ≤ 5 from the full recording; invalid -> contract error); URLs are built by code; the
block between ⟦vp-links⟧…⟦/vp-links⟧ in each description is replaced via read-modify-write
(the rest of the text stays as it is on the platform); if the description would exceed the
YouTube limit, the lowest-priority links are dropped (related, then prev/next/fragment).

Acceptance: every link points to an uploaded video of this series that exists on the platform;
the block is present in every description; the description limit holds; the vp tag survives
(Publisher.update_snippet verifies it).

Output: crosslinks.yaml {links: [...], descriptions: {item: {video_id, description, block}}}.
Config: s10.max_links (5).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.io import read_yaml, sha256_bytes, write_yaml
from ..core.paths import FULL
from ..core.timefmt import fmt_clock
from . import _limits
from ._package import (extract_links_block, fragment_info, item_meta, link_video_id, load_uploads,
                       platform_video_url, replace_links_block, series_items, uploaded_ids)
from .base import Ctx, StepSpec, VPError

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "s10_crosslinks.md"
TASK = "s10.graph"
KINDS = ("fragment", "next", "prev", "related")
_LABEL = {"next": "Дальше", "prev": "Раньше", "related": "По теме", "fragment": "Фрагмент"}
_DROP_ORDER = ("related", "prev", "next", "fragment")


def _max_links(ctx: Ctx) -> int:
    return int((ctx.config.get("s10") or {}).get("max_links", 5))


def numbering(ctx: Ctx) -> dict[int, str]:
    """Number -> item: 0 = full recording, 1..n fragments in recording order."""
    items = [i for i in series_items(ctx.job) if i != FULL]
    out = {0: FULL}
    for n, it in enumerate(items, 1):
        out[n] = it
    return out


def items_text(ctx: Ctx, nums: dict[int, str]) -> str:
    """What the model sees: number, type, order, title, thesis. No ids, URLs or times."""
    info = fragment_info(ctx.job)
    lines = []
    for n, it in sorted(nums.items()):
        rec = info.get(it) or {}
        try:
            title = item_meta(ctx.job, it)["title"]
        except VPError:
            title = rec.get("title", "")
        thesis = " ".join(str(rec.get("thesis") or "").split())
        lines.append(f"[{n}] type: {rec.get('kind', 'reel')} | order: {rec.get('order', n)} | "
                     f"title: {' '.join(title.split())} | thesis: {thesis}")
    return "\n".join(lines) + "\n"


def parse_links(text: str, nums: dict[int, str], max_links: int = 5) -> list[dict[str, Any]]:
    """Validate the model output; returns [{from, to, kind}] or raises VPError(contract)."""
    raw = text.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise VPError("contract", "s10.graph: no JSON object in the output")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise VPError("contract", f"s10.graph: invalid JSON ({e.msg})") from None
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, list):
        raise VPError("contract", "s10.graph: `links` must be a list")
    seen: set[tuple[int, int]] = set()
    per_src: dict[int, int] = {}
    out = []
    for ln in links:
        if not isinstance(ln, dict) or set(ln) - {"from", "to", "kind"}:
            raise VPError("contract", f"s10.graph: bad link {ln!r}")
        a, b, kind = ln.get("from"), ln.get("to"), ln.get("kind")
        if not isinstance(a, int) or not isinstance(b, int) or isinstance(a, bool) or isinstance(b, bool):
            raise VPError("contract", f"s10.graph: link numbers must be integers: {ln!r}")
        if a not in nums or b not in nums:
            raise VPError("contract", f"s10.graph: unknown item number in {ln!r}")
        if a == b:
            raise VPError("contract", f"s10.graph: self link {ln!r}")
        if kind not in KINDS:
            raise VPError("contract", f"s10.graph: unknown kind {kind!r}")
        if b == 0:
            raise VPError("contract", "s10.graph: links to the full recording are automatic")
        if (kind == "fragment") != (a == 0):
            raise VPError("contract", f"s10.graph: kind `fragment` is only for links from [0]: {ln!r}")
        if (a, b) in seen:
            raise VPError("contract", f"s10.graph: duplicate link {a}->{b}")
        seen.add((a, b))
        per_src[a] = per_src.get(a, 0) + 1
        limit = max_links if a == 0 else max_links - 1
        if per_src[a] > limit:
            raise VPError("contract", f"s10.graph: more than {limit} links from [{a}]")
        out.append({"from": a, "to": b, "kind": kind})
    return out


def _title_for_line(ctx: Ctx, item: str) -> str:
    try:
        t = item_meta(ctx.job, item)["title"]
    except VPError:
        t = item
    t = " ".join(t.replace("<", "").replace(">", "").split())
    return t[:80]


def build_lines(ctx: Ctx, nums: dict[int, str], links: list[dict[str, Any]],
                ids: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Per source item: ordered link records {to, kind, url, line, auto}."""
    info = fragment_info(ctx.job)
    man = read_yaml(ctx.job.render_manifest(FULL), default={}) or {}
    intro = int(man.get("intro_ms") or 0)
    out: dict[str, list[dict[str, Any]]] = {it: [] for it in nums.values()}
    for it in nums.values():
        if it == FULL:
            continue
        t_ms = int((info.get(it) or {}).get("start_ms") or 0) + intro
        url = platform_video_url(ctx.publisher, ids[FULL], t_ms or None)
        line = f"Полная запись (с {fmt_clock(t_ms)}): {_title_for_line(ctx, FULL)} — {url}"
        out[it].append({"to": FULL, "kind": "full", "url": url, "line": line, "auto": True})
    for ln in links:
        src, dst = nums[ln["from"]], nums[ln["to"]]
        url = platform_video_url(ctx.publisher, ids[dst])
        line = f"{_LABEL[ln['kind']]}: {_title_for_line(ctx, dst)} — {url}"
        out[src].append({"to": dst, "kind": ln["kind"], "url": url, "line": line, "auto": False})
    order = {"full": 0, "fragment": 1, "next": 2, "prev": 3, "related": 4}
    for src in out:
        out[src].sort(key=lambda r: order[r["kind"]])
    return out


def fit_block(base: str, recs: list[dict[str, Any]], limit: int) -> tuple[str, list[dict[str, Any]]]:
    """Description with the block; drops low-priority links until it fits `limit`."""
    recs = list(recs)
    for kind in _DROP_ORDER + (None,):
        while True:
            desc = replace_links_block(base, [r["line"] for r in recs])
            if len(desc) <= limit:
                return desc, recs
            victims = [i for i, r in enumerate(recs) if not r["auto"] and (kind is None or r["kind"] == kind)]
            if not victims:
                break
            recs.pop(victims[-1])
    raise VPError("data", "description too long even without cross-links")


class S10Crosslinks(StepSpec):
    id, deps, scope, version, gate = "S10", ("S8",), "job", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j = ctx.job
        out = [j.fragments_yaml, j.render_manifest(FULL)]
        for it in j.items() or []:
            out += [j.meta_yaml(it), j.render_mp4(it)]
        return out

    def config(self, ctx: Ctx) -> dict:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        return {"prompt_sha": sha256_bytes(prompt), "max_links": _max_links(ctx),
                "description_max": _limits.DESCRIPTION_MAX}

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.crosslinks_yaml]

    def run(self, ctx: Ctx) -> dict:
        job = ctx.job
        nums = numbering(ctx)
        ids = uploaded_ids(job)
        missing = [it for it in nums.values() if it not in ids]
        if missing:
            raise VPError("data", f"S10: not uploaded yet: {missing}")
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        raw = ctx.judge.run(TASK, prompt, {"items": items_text(ctx, nums)}, "output.json", ctx)
        links = parse_links(raw, nums, _max_links(ctx))
        per_item = build_lines(ctx, nums, links, ids)

        pub = ctx.publisher
        series_ids = set(ids[it] for it in nums.values())
        exists: dict[str, bool] = {}

        def check_exists(vid: str) -> bool:
            if vid not in exists:
                exists[vid] = ctx.retry(lambda: pub.get_video(vid)) is not None
            return exists[vid]

        descriptions: dict[str, Any] = {}
        all_links = []
        written = 0
        held: list[str] = []
        entries = load_uploads(job)["items"]
        for it in sorted(nums.values(), key=lambda i: (i == FULL, i)):
            vid = ids[it]
            video = ctx.retry(lambda: pub.get_video(vid))
            if video is None:
                raise VPError("data", f"S10: video of {it} not found on the platform")
            exists[vid] = True
            # A published video is never changed without a new approval: its new description is
            # built from the local meta and only goes into the package (PUB applies it once the
            # owner approves that package).
            frozen = bool((entries.get(it) or {}).get("published"))
            base = (item_meta(job, it)["description"] if frozen
                    else (video.get("snippet") or {}).get("description") or "")
            new, kept = fit_block(base, per_item[it], _limits.DESCRIPTION_MAX)
            for r in kept:
                tvid = link_video_id(r["line"])
                if tvid not in series_ids or not check_exists(tvid):
                    raise VPError("data", f"S10: link from {it} points outside the series")
            current = (video.get("snippet") or {}).get("description") or ""
            if frozen:
                if new != current:
                    held.append(it)
            elif new != base:
                ctx.retry(lambda: pub.update_snippet(vid, {"description": new}))
                written += 1
            if extract_links_block(new) is None or len(new) > _limits.DESCRIPTION_MAX:
                raise VPError("internal", f"S10: block missing or too long for {it}")
            descriptions[it] = {"video_id": vid, "description": new,
                                "block": [r["line"] for r in kept]}
            all_links += [{"from": it, "to": r["to"], "kind": r["kind"], "url": r["url"],
                           "line": r["line"]} for r in kept]
        dropped = sum(len(per_item[it]) for it in per_item) - len(all_links)
        write_yaml(job.crosslinks_yaml, {"version": 1, "numbering": {str(k): v for k, v in nums.items()},
                                         "links": all_links, "descriptions": descriptions})
        return {"links": len(all_links), "dropped_for_length": dropped, "updated": written,
                "held_for_approval": held}


STEP = S10Crosslinks()
