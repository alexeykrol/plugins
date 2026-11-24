"""Compile a parsed long plan into the frozen editorial and execution clocks."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Literal, TypedDict

from ..analysis.edit_anchors import AnchorResolver, Bounds, ResolvedWord, WordTiming
from ..analysis.edit_clock import EditClock, EditOccurrence, Gap, KeptSpan
from ..builders.contracts import (
    Canvas, CutResolution, LongPlan, LongTimeline, OutputSpan, Ratio, SourceSpan,
    TimelineCaption, TimelineCard, TimelineElement, TimelineInputs, TimelineOverlay, TimelineWord,
    normalize_long_plan, plan_digest, validate_long_plan, validate_timeline,
)
from ..core.errors import VPError
from ..core.io import sha256_obj
from ..core.srt import Cue

COMPILER_REVISION = "long-compiler-v1"
PRESET_REVISION = "owner_long_v1"
HOOK_MS = 10_000
OUTRO_MS = 30_000
OUTRO_POINTS_MS = (1_000, 7_000, 13_000, 19_000)
OUTRO_SUBLINE_MS = 25_000
FLASH_FRAMES_AT_25 = 4
PICTURE_FRAMES_AT_25 = 3
AUDIO_FADE_MS = 60
TIMELAPSE_SPLIT_MS = 60_000
TIMELAPSE_SHORT_MS = 2_000
TIMELAPSE_LONG_MS = 3_000
_COMPILE_INPUT = "long_compile"


@dataclass(frozen=True)
class LongCompilePolicy:
    canvas: Canvas
    frame_rate: Ratio
    sample_rate: int
    max_cut_shift_ms: int
    hook_end_text_lead_ms: int


class ChapterMarker(TypedDict):
    id: str
    start_ms: int
    title: str


@dataclass(frozen=True)
class _Grid:
    frame_ms: Fraction
    sample_rate: int
    source_id: str

    def time(self, frames: Bounds) -> tuple[Fraction, Fraction]:
        return frames[0] * self.frame_ms, frames[1] * self.frame_ms

    def source(self, start: int | Fraction, end: int | Fraction,
               frames: Bounds | None = None) -> SourceSpan:
        source: SourceSpan = {"source_id": self.source_id, "samples": {
            "start": round(Fraction(start) * self.sample_rate / 1000),
            "end": round(Fraction(end) * self.sample_rate / 1000),
        }}
        if frames is not None:
            source["frames"] = {"start": frames[0], "end": frames[1]}
        return source


@dataclass(frozen=True)
class _Speech:
    cue: Cue
    words: tuple[ResolvedWord, ...]

    def protection(self):
        # Untimed or zero-length tokens cannot certify the interior of a quiet interval.
        if not self.words or any(w.start.ms == w.end.ms for w in self.words):
            yield self.cue.start_ms, self.cue.end_ms, f"cue {self.cue.id}"
        for word in self.words:
            if word.start.ms < word.end.ms:
                yield word.start.ms, word.end.ms, word.id


@dataclass(frozen=True)
class _Section:
    id: str
    kind: Literal["hook", "teaser", "body", "timelapse", "outro"]
    frames: Bounds | None
    card: TimelineCard | None = None
    duration_ms: int | None = None


def _ratio(ratio: Ratio) -> Fraction:
    if any(type(ratio[k]) is not int or ratio[k] <= 0 for k in ("num", "den")):
        raise VPError("data", "expected a positive integer clock ratio")
    return Fraction(ratio["num"], ratio["den"])


def _inside(span, envelope) -> bool:
    return envelope[0] <= span[0] < span[1] <= envelope[1]


def _overlaps(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _kept_frames(bounds: Bounds, approvals: Sequence[Bounds], grid: _Grid,
                 protected: Sequence[tuple[int, int, str]], path: str) -> Bounds:
    start, end = bounds
    frame_ms = grid.frame_ms
    available = []
    for low, high in approvals:
        if not _inside(bounds, (low, high)):
            continue
        # Include every word intersecting the requested speech. Only approved
        # source frames can supply handles around those words.
        intersecting = [(a, b) for a, b, _ in protected if _overlaps((a, b), bounds)]
        needed_start = min((a for a, _ in intersecting), default=start)
        needed_end = max((b for _, b in intersecting), default=end)
        left = max(floor(min(start, needed_start) / frame_ms), ceil(low / frame_ms))
        right = min(ceil(max(end, needed_end) / frame_ms), floor(high / frame_ms))
        while left < right:
            split = [a for a, b, _ in protected if a < left * frame_ms < b]
            if not split:
                break
            left = floor(min(split) / frame_ms)
        while left < right:
            split = [b for a, b, _ in protected if a < right * frame_ms < b]
            if not split:
                break
            right = ceil(max(split) / frame_ms)
        if (ceil(low / frame_ms) <= left < right <= floor(high / frame_ms)
                and all(left * frame_ms <= a and b <= right * frame_ms for a, b in intersecting)):
            available.append((left, right))
    if not available:
        raise VPError("data", f"{path}: no approved frame bounds preserve the selected speech")
    return min(available, key=lambda pair: pair[1] - pair[0])


def _cut_ranges(plan, resolver, body_ref, body_frames, grid, quiet, protected, measured_edges, max_shift):
    proposed = []
    for i, cut in enumerate(plan["cuts"]):
        resolved = resolver.resolve_range(cut["range"], path=f"cuts[{i}].range", envelope=body_ref)
        proposed.append(((resolved.start_ms, resolved.end_ms), cut))
    proposed.sort(key=lambda row: row[0])
    for a, b in zip(proposed, proposed[1:]):
        if _overlaps(a[0], b[0]):
            raise VPError("data", f"overlapping proposed cuts: {a[1]['id']} and {b[1]['id']}")

    resolutions: dict[str, CutResolution] = {}
    applied = []
    frame_ms = grid.frame_ms

    def edge_candidates(target, *, opening):
        # Inspect only quiet frame positions within the allowed displacement.
        candidates = set()
        for low, high in quiet:
            first = max(body_frames[0], ceil(max(low, target - max_shift) / frame_ms))
            last = min(body_frames[1], floor(min(high, target + max_shift) / frame_ms))
            candidates.update(range(first, last + 1))
        aligned = Fraction(target) / frame_ms
        if (aligned.denominator == 1 and body_frames[0] <= aligned <= body_frames[1]
                and (target in measured_edges or aligned in body_frames)):
            candidates.add(int(aligned))
        def safe(frame):
            point = frame * frame_ms
            if any(a < point < b for a, b, _ in protected):
                return False
            # Moving outwards must not expand a deliberate removal into
            # neighboring speech, even when the new edge is itself quiet.
            spill = (point, target) if opening else (target, point)
            return spill[0] >= spill[1] or not any(
                _overlaps(spill, (a, b)) for a, b, _ in protected
            )

        return sorted((n for n in candidates if safe(n)),
                      key=lambda n: (abs(n * frame_ms - target), n))

    for (start, end), cut in proposed:
        action, cut_id = cut["action"], cut["id"]
        request = grid.source(start, end)
        actual = None
        evidence = [f"resolved source range {start}..{end} ms"]
        if action == "shorten_pause":
            blocked = [word for a, b, word in protected if _overlaps((a, b), (start, end))]
            if blocked:
                reason = "protected_speech_in_proposed_pause"
                evidence.append("protected speech: " + ", ".join(blocked))
            else:
                candidates = []
                for low, high in quiet:
                    left = max(body_frames[0], ceil(max(low, start) / frame_ms))
                    right = min(body_frames[1], floor(min(high, end) / frame_ms))
                    if (left < right and left * frame_ms - start <= max_shift
                            and end - right * frame_ms <= max_shift):
                        candidates.append((left, right))
                if candidates:
                    actual = max(candidates, key=lambda frames: frames[1] - frames[0])
                    reason = "removed_verified_quiet_interior"
                    evidence.append(f"quiet coverage and frame snapping: {grid.time(actual)} ms")
                else:
                    reason = "no_verified_quiet_interior_within_shift_limit"
        else:
            starts, ends = edge_candidates(start, opening=True), edge_candidates(end, opening=False)
            # For each start, only its closest feasible end can win.
            pairs = ((left, right) for left in starts
                     if (right := next((candidate for candidate in ends if left < candidate), None)) is not None)
            actual = min(pairs, key=lambda pair: abs(pair[0] * frame_ms - start)
                         + abs(pair[1] * frame_ms - end), default=None)
            if actual is None:
                raise VPError("data", f"cut {cut_id}: no speech-safe frame edges within the shift limit")
            reason = "editorial_cut_at_speech_safe_frame_edges"
            evidence.append(f"frame edges supported by measured quiet, measured words or body bounds: "
                            f"{grid.time(actual)} ms")
        resolved = None
        if actual is not None:
            resolved = grid.source(*grid.time(actual), frames=actual)
            applied.append((actual, cut))
        resolutions[cut_id] = {
            "id": cut_id, "action": action, "proposed": request, "actual": resolved,
            "start_shift_samples": None if resolved is None else resolved["samples"]["start"] - request["samples"]["start"],
            "end_shift_samples": None if resolved is None else resolved["samples"]["end"] - request["samples"]["end"],
            "reason": reason, "evidence": evidence,
        }
    applied.sort(key=lambda row: row[0])
    for a, b in zip(applied, applied[1:]):
        if _overlaps(a[0], b[0]):
            raise VPError("data", f"overlapping resolved cuts: {a[1]['id']} and {b[1]['id']}")
    return [resolutions[cut["id"]] for cut in plan["cuts"]], applied


def _span(clock: EditClock, start_frame: int, end_frame: int) -> OutputSpan:
    return {"frames": {"start": start_frame, "end": end_frame}, "samples": {
        "start": clock.sample_at_frame(start_frame), "end": clock.sample_at_frame(end_frame),
    }}


def _captions(section: _Section, occurrence: EditOccurrence, speech: Sequence[_Speech], grid: _Grid):
    start, end = grid.time(section.frames)
    words: list[TimelineWord] = []
    captions: list[TimelineCaption] = []
    for row in speech:
        cue = row.cue
        timed = [w for w in row.words if w.start.ms < w.end.ms]
        selected = [w for w in timed if start <= w.start.ms < w.end.ms <= end]
        if not _overlaps((start, end), (cue.start_ms, cue.end_ms)) and not selected:
            continue
        whole = start <= cue.start_ms and cue.end_ms <= end and len(timed) == len(selected)
        caption_words = []
        for word in selected:
            output = occurrence.project_range(word.start.ms, word.end.ms)
            if output["samples"]["start"] == output["samples"]["end"]:
                continue
            matched = word.start.provenance == word.end.provenance == "measured"
            entry: TimelineWord = {
                "element_id": section.id, "cue_id": cue.id, "word_id": word.id, "text": word.text,
                "provenance": "matched" if matched else "interpolated", "output": output,
                "source_samples": grid.source(word.start.ms, word.end.ms)["samples"],
            }
            if matched and word.start.source_word_id:
                entry["source_word_id"] = word.start.source_word_id
            words.append(entry)
            caption_words.append(entry)
        if whole:
            text = cue.text.strip()
            low = min([cue.start_ms, *(word.start.ms for word in selected)])
            high = max([cue.end_ms, *(word.end.ms for word in selected)])
        elif selected:
            first, last = row.words.index(selected[0]), row.words.index(selected[-1])
            text = (cue.text.strip() if len(selected) == len(timed)
                    else " ".join(word.text for word in row.words[first:last + 1]))
            low = min(word.start.ms for word in selected)
            high = max(word.end.ms for word in selected)
        else:
            continue
        if low >= high:
            continue
        span = occurrence.project_range(low, high)
        if span["samples"]["start"] < span["samples"]["end"]:
            ids = [w["word_id"] for w in caption_words]
            captions.append({
                "element_id": section.id, "cue_id": cue.id, "mode": "word" if ids else "phrase",
                "text": text, "output": span, "word_ids": ids,
            })
    captions.sort(key=lambda row: row["output"]["samples"]["start"])
    # Prefer the adjacent word boundaries where subtitle display padding overlaps.
    for left, right in zip(captions, captions[1:]):
        for axis in ("frames", "samples"):
            overlap = left["output"][axis]["end"] > right["output"][axis]["start"]
            if not overlap:
                continue
            left_words = [w["output"][axis]["end"] for w in words
                          if w["cue_id"] == left["cue_id"]]
            right_words = [w["output"][axis]["start"] for w in words
                           if w["cue_id"] == right["cue_id"]]
            if not left_words or not right_words or max(left_words) > min(right_words):
                raise VPError("data", f"overlapping subtitle speech: {left['cue_id']} and {right['cue_id']}")
            seam = min(right_words)
            left["output"][axis]["end"] = right["output"][axis]["start"] = seam
    return words, captions


def _audio_fade(occurrence, protected, *, incoming):
    kept = occurrence.span
    start, end = kept.start_ms, kept.end_ms
    speech = [(a, b) for a, b, _ in protected if _overlaps((start, end), (a, b))]
    if incoming:
        low = start
        high = min(start + AUDIO_FADE_MS, end, min((a for a, _ in speech), default=end))
    else:
        low = max(end - AUDIO_FADE_MS, start, max((b for _, b in speech), default=start))
        high = end
    if low < high:
        span = occurrence.project_range(low, high)
        if span["samples"]["start"] < span["samples"]["end"]:
            return {"kind": "audio_fade_in" if incoming else "audio_fade_out", "output": span}
    return None


def compile_long_plan(
    plan: LongPlan, accepted: Mapping[str, object], cues: Sequence[Cue],
    word_times: Mapping[str, Sequence[WordTiming]] | None, *,
    inputs: TimelineInputs, plan_transcript_sha256: str,
    word_times_transcript_sha256: str | None, source_id: str, source_frames: int,
    fragments: Mapping[str, Bounds], quiet_spans_ms: Sequence[Bounds],
    policy: LongCompilePolicy,
) -> LongTimeline:
    """Compile from current artifact digests and accepted millisecond ranges.

    `fragments` contains separately approved excerpts. Quiet intervals must be
    measured on this source by the caller using its accepted detector policy.
    Bind the detector policy/version in inputs.analysis if applicable.
    """
    errors = validate_long_plan(plan)
    if errors:
        raise VPError("data", "\n".join(errors))
    plan = normalize_long_plan(plan)
    if inputs["plan"] != plan_digest(plan) or inputs["transcript"] != plan_transcript_sha256:
        raise VPError("data", "stale long plan: plan or authored transcript fingerprint differs")
    if word_times and (inputs["source_words"] is None or word_times_transcript_sha256 != inputs["transcript"]):
        raise VPError("data", "stale word times: source word digest and current transcript binding required")
    if accepted.get("id") != plan["item"] or accepted.get("type") not in ("long", "full"):
        raise VPError("data", "long plan must name an accepted long or full item")
    if source_id not in inputs["sources"] or type(source_frames) is not int or source_frames <= 0:
        raise VPError("data", "a measured source ID and positive frame count are required")
    for label, value in (
        ("sample_rate", policy.sample_rate), ("max_cut_shift_ms", policy.max_cut_shift_ms),
        ("hook_end_text_lead_ms", policy.hook_end_text_lead_ms),
    ):
        if type(value) is not int or value < 0 or (label != "max_cut_shift_ms" and value == 0):
            raise VPError("data", f"invalid long compile policy: {label}")
    fps = _ratio(policy.frame_rate)
    source = inputs["sources"][source_id]
    if type(source["sample_rate"]) is not int or source["sample_rate"] <= 0:
        raise VPError("data", "source needs a positive integer sample rate")
    grid = _Grid(1000 / _ratio(source["frame_rate"]), source["sample_rate"], source_id)
    limit = source_frames * grid.frame_ms
    body_approval = accepted.get("start_ms"), accepted.get("end_ms")
    approved = {**fragments, plan["item"]: body_approval}
    for label, bounds in [*approved.items(), *(("quiet span", span) for span in quiet_spans_ms)]:
        if len(bounds) != 2 or any(type(edge) is not int for edge in bounds) or not _inside(bounds, (0, limit)):
            raise VPError("data", f"{label}: invalid source bounds")
    quiet = []
    for start, end in sorted(quiet_spans_ms):
        if quiet and start <= quiet[-1][1]:
            quiet[-1] = quiet[-1][0], max(end, quiet[-1][1])
        else:
            quiet.append((start, end))

    resolver = AnchorResolver(
        cues, transcript_sha256=inputs["transcript"], plan_transcript_sha256=plan_transcript_sha256,
        word_times=word_times, word_times_transcript_sha256=word_times_transcript_sha256, fragments=approved,
    )
    speech = [_Speech(cue, resolver.words_for_cue(cue.id)) for cue in cues if cue.text.strip()]
    protected = [row for entry in speech for row in entry.protection()]
    measured_edges = {
        edge.ms for row in speech for word in row.words if word.start.ms < word.end.ms
        for edge in (word.start, word.end) if edge.provenance == "measured"
    }
    body = resolver.resolve_range(plan.get("body", {"fragment": plan["item"]}), path="body",
                                  envelope=body_approval)
    body_ref = body.start_ms, body.end_ms
    body_frames = _kept_frames(body_ref, [body_approval], grid, protected, "body")
    cuts, applied = _cut_ranges(plan, resolver, body_ref, body_frames, grid, quiet, protected, measured_edges,
                                policy.max_cut_shift_ms)

    def excerpt(ref, path):
        resolved = resolver.resolve_range(ref, path=path)
        approvals = [approved[resolved.fragment_id]] if resolved.fragment_id else list(approved.values())
        return _kept_frames((resolved.start_ms, resolved.end_ms), approvals, grid, protected, path)

    sections = []
    if hook := plan.get("hook"):
        card: TimelineCard = {"text": hook["title"]}
        if "subtitle" in hook:
            card["subtitle"] = hook["subtitle"]
        sections.append(_Section("hook", "hook", excerpt(hook["range"], "hook.range"), card))
    for i, ref in enumerate(plan["teaser"], 1):
        sections.append(_Section(f"teaser-{i}", "teaser", excerpt(ref, f"teaser[{i - 1}]")))
    cursor = body_frames[0]
    number = 0
    for (start, end), cut in applied:
        if cursor < start:
            number += 1
            sections.append(_Section(f"body-{number}", "body", (cursor, start)))
        if cut["action"] == "timelapse":
            duration = TIMELAPSE_LONG_MS if (end - start) * grid.frame_ms > TIMELAPSE_SPLIT_MS else TIMELAPSE_SHORT_MS
            sections.append(_Section(f"timelapse-{cut['id']}", "timelapse", (start, end),
                                     {"text": cut["label"]}, duration))
        cursor = end
    if cursor < body_frames[1]:
        sections.append(_Section(f"body-{number + 1}", "body", (cursor, body_frames[1])))
    if outro := plan.get("outro"):
        card = {"text": outro["title"], "points": [
            {"text": value, "revealFrame": round(ms * fps / 1000)}
            for value, ms in zip(outro["points"], OUTRO_POINTS_MS)
        ]}
        if "subline" in outro:
            card["subline"] = {"text": outro["subline"], "revealFrame": round(OUTRO_SUBLINE_MS * fps / 1000)}
        sections.append(_Section("outro", "outro", None, card, OUTRO_MS))

    spans = []
    for section in sections:
        if section.frames is None:
            spans.append(Gap(section.duration_ms, section.id))
            continue
        start, end = grid.time(section.frames)
        rate = (end - start) / section.duration_ms if section.kind == "timelapse" else Fraction(1)
        spans.append(KeptSpan(source_id, start, end, section.id, rate))
        if section.kind == "hook":
            hold = HOOK_MS - (end - start)
            if round(HOOK_MS * fps / 1000) > round((end - start) * fps / 1000):
                spans.append(Gap(hold, "hook-hold"))
    try:
        clock = EditClock(spans, frame_rate=policy.frame_rate, sample_rate=policy.sample_rate)
    except ValueError as exc:
        raise VPError("data", str(exc)) from exc
    occurrences = {row.id: row for row in clock.occurrences}
    elements: list[TimelineElement] = []
    words: list[TimelineWord] = []
    captions: list[TimelineCaption] = []
    for section in sections:
        occurrence = occurrences[section.id]
        hold = occurrences.get("hook-hold") if section.kind == "hook" else None
        frames = occurrence.start_frame, (hold or occurrence).end_frame
        duration = frames[1] - frames[0]
        rate = Fraction(occurrence.span.rate) if isinstance(occurrence.span, KeptSpan) else Fraction(1)
        silent = section.kind in ("timelapse", "outro")
        element: TimelineElement = {
            "id": section.id, "kind": section.kind, "output": _span(clock, *frames),
            "rate": {"num": rate.numerator, "den": rate.denominator},
            "audio": "silent" if silent else "source", "events": [],
        }
        if section.frames is not None:
            element["source"] = grid.source(*grid.time(section.frames), frames=section.frames)
        if section.card is not None:
            element["template"] = section.kind
            element["card"] = section.card
        if section.kind == "hook" and "end_text" in plan["hook"]:
            lead = max(1, round(policy.hook_end_text_lead_ms * fps / 1000))
            element["card"]["end_text"] = {"text": plan["hook"]["end_text"], "revealFrame": max(0, duration - lead)}
        if hold is not None:
            element["events"].extend([
                {"kind": "freeze", "output": hold.output, "source_frame": section.frames[1] - 1},
                {"kind": "silence", "output": hold.output},
            ])
        if silent:
            element["events"].append({"kind": "silence", "output": element["output"]})
        else:
            local_words, local_captions = _captions(section, occurrence, speech, grid)
            words.extend(local_words)
            captions.extend(local_captions)
            for a, b, _ in protected:
                if _overlaps((a, b), (occurrence.span.start_ms, occurrence.span.end_ms)):
                    speech_span = occurrence.project_range(a, b)
                    if speech_span["samples"]["start"] < speech_span["samples"]["end"]:
                        element["events"].append({"kind": "speech", "output": speech_span})
        if elements:
            previous = elements[-1]
            if previous["kind"] in ("hook", "teaser") and section.kind in ("teaser", "body", "timelapse"):
                transition = "flash"
                count = round(FLASH_FRAMES_AT_25 * fps / 25)
            elif previous["kind"] in ("body", "timelapse") and section.kind in ("body", "timelapse"):
                transition = "picture"
                count = round(PICTURE_FRAMES_AT_25 * fps / 25)
                for entry, incoming in [(previous, False), (element, True)]:
                    if entry["audio"] == "source":
                        fade = _audio_fade(occurrences[entry["id"]], protected, incoming=incoming)
                        if fade:
                            entry["events"].append(fade)
            else:
                count = 0
            if count:
                element["transition"] = {"kind": transition,
                                         "output": _span(clock, frames[0], frames[0] + min(duration, count))}
        elements.append(element)

    body_occurrences = [occurrences[el["id"]] for el in elements if el["kind"] == "body"]
    retained = [(occ.span.start_ms, occ.span.end_ms) for occ in body_occurrences]
    overlays: list[TimelineOverlay] = []
    for key, kind in (("chapters", "chapter"), ("subscribe", "subscribe")):
        for i, overlay in enumerate(plan[key]):
            at = overlay["at"]
            edge = resolver.resolve_anchor(at, path=f"{key}[{i}].at",
                                           require_measured="word_id" in at or "phrase" in at, retained=retained)
            owner = next(occ for occ in body_occurrences if occ.span.start_ms <= edge.ms < occ.span.end_ms)
            position, = clock.source_to_output(source_id, edge.ms, occurrence_id=owner.id)
            first = position.frame
            last = min(clock.duration_frames, first + round(overlay["duration_ms"] * fps / 1000))
            if not owner.start_frame <= first < owner.end_frame or first >= last:
                raise VPError("data", f"{key}[{i}].at: overlay has no frame at its retained anchor")
            entry: TimelineOverlay = {
                "id": overlay["id"], "kind": kind, "element_id": owner.id, "cue_id": edge.cue_id,
                "text": overlay["text"], "output": _span(clock, first, last),
            }
            if edge.word_id is not None:
                entry["word_id"] = edge.word_id
            overlays.append(entry)
    overlays.sort(key=lambda row: row["output"]["frames"]["start"])
    fingerprints = deepcopy(inputs)
    fingerprints.setdefault("analysis", {})[_COMPILE_INPUT] = sha256_obj({
        "source_id": source_id, "source_frames": source_frames, "approved": approved,
        "policy": asdict(policy), "quiet": quiet, "accepted_item": dict(accepted),
        "cues": [asdict(cue) for cue in cues],
        "word_times": {key: [asdict(word) for word in values] for key, values in (word_times or {}).items()},
    })
    timeline: LongTimeline = {
        "version": 1, "kind": "long", "item": plan["item"], "preset": plan["preset"],
        "preset_revision": PRESET_REVISION, "compiler_revision": COMPILER_REVISION,
        "inputs": fingerprints, "canvas": dict(policy.canvas), "frame_rate": dict(policy.frame_rate),
        "sample_rate": policy.sample_rate, "duration_frames": clock.duration_frames,
        "duration_samples": clock.duration_samples, "elements": elements, "words": words,
        "captions": captions, "cuts": cuts, "overlays": overlays, "video_units": [
            {"id": f"unit-{element['id']}", "output": deepcopy(element["output"]), "element_ids": [element["id"]]}
            for element in elements
        ],
    }
    errors = validate_timeline(timeline)
    if errors:
        raise VPError("data", "\n".join(errors))
    long_srt_cues(timeline)  # A compiled sidecar also needs positive durations after SRT quantization.
    return timeline


def long_srt_cues(timeline: LongTimeline) -> list[Cue]:
    """Project the compiled sample clock into integer-millisecond SRT cues."""
    result = []
    for i, row in enumerate(timeline["captions"], 1):
        samples = row["output"]["samples"]
        start, end = (round(Fraction(samples[edge] * 1000, timeline["sample_rate"])) for edge in ("start", "end"))
        if start == end:
            raise VPError("data", f"SRT cue {row['cue_id']} has no duration on the millisecond clock")
        result.append(Cue(str(i), start, end, row["text"]))
    return result


def long_chapters(timeline: LongTimeline) -> list[ChapterMarker]:
    """Return local markers at the same output frames as the chapter overlays."""
    period = 1000 / _ratio(timeline["frame_rate"])
    return [
        {"id": row["id"], "start_ms": round(row["output"]["frames"]["start"] * period), "title": row["text"]}
        for row in timeline["overlays"] if row["kind"] == "chapter"
    ]
