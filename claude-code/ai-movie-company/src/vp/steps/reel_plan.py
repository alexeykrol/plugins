"""Compile accepted editorial references into the local reel timeline contract."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import TypedDict

from ..adapters import media
from ..adapters.reel_cut import ReelSource
from ..analysis.align_words import norm, paged
from ..analysis.edit_anchors import (
    AnchorError, AnchorResolver, Bounds, ResolvedRange, ResolvedWord, WordTiming,
)
from ..analysis.edit_clock import EditClock, Gap, KeptSpan, EditOccurrence
from ..builders.contracts import (
    Anchor, Crop, OutputSpan, Ratio, Rect, ReelItem, ReelTimeline, ReelsPlan,
    RenderBox, TimelineCaption, TimelineElement, TimelineEvent, TimelineLayout, TimelineWord,
    normalize_reel_item, plan_digest, validate_reels, validate_timeline,
)
from ..builders.presets import REEL_PRESETS, ReelPreset, RequiredReel810Geometry
from ..core.io import sha256_obj
from ..core.srt import Cue

COMPILER_REVISION = "reel-plan-2"


class EffectStyle(TypedDict, total=False):
    slot: str
    box: RenderBox
    scale: float
    amplitudePx: float


@dataclass(frozen=True)
class ReelCompileSettings:
    sample_rate: int
    audio_close_ms: int
    intro_readable_dwell_frames: int
    thought_readable_dwell_frames: int
    default_effect_duration_ms: Mapping[str, int]
    split_810: RequiredReel810Geometry | None = None
    effect_styles: Mapping[str, EffectStyle] = field(default_factory=dict)
    effect_assets: Mapping[str, str] = field(default_factory=dict)
    speaker_background: bool = False


@dataclass(frozen=True)
class _Piece:
    id: str
    path: str
    start: int
    end: int
    open_at: int
    open_until: int | Fraction
    close_from: int
    close_at: int
    picture_start: int
    picture_end: int
    lead: int
    tail: int
    fade_in: int
    fade_out: int
    words: tuple[ResolvedWord, ...]
    cues: tuple[Cue, ...]
    caption_cue_ids: frozenset[str]

    @property
    def duration(self) -> int:
        return self.lead + self.end - self.start + self.tail


def measure_reel_pauses(source_path: str | Path, *, preset: ReelPreset) -> list[Bounds]:
    """Measure source-relative quiet spans with the preset's shortest detector window."""
    raw = media.silences(source_path, noise_db=preset.silence_db,
                         min_s=min(preset.pause_lengths_ms) / 1000)
    # The adapter truncates both endpoints to milliseconds. Round inward.
    return [(a + bool(a), b) for a, b in raw if a + bool(a) < b]


def _check(errors: list[str]) -> None:
    if errors:
        path, _, detail = errors[0].partition(": ")
        raise AnchorError(path, detail)


def _positive(value: int, path: str) -> None:
    if type(value) is not int or value <= 0:
        raise AnchorError(path, "expected a positive integer")


def _rect(rect: Rect, source: ReelSource, path: str, *, crop: bool = True) -> None:
    x, y, w, h = (rect[key] for key in ("x", "y", "width", "height"))
    if (any(type(v) is not int for v in (x, y, w, h)) or min(x, y) < 0
            or min(w, h) <= 0 or x + w > source.width or y + h > source.height):
        raise AnchorError(path, "crop lies outside the source picture")
    if crop and any(value % 2 for value in (x, y, w, h)):
        raise AnchorError(path, "crop coordinates and dimensions must be even for yuv420p")


def _screen(crop: Crop, source: ReelSource, path: str) -> dict:
    screen = crop["screen"]
    _rect(screen, source, f"{path}.screen")
    result = {"screen": screen}
    if "screen_bounds" in crop:
        bounds = crop["screen_bounds"]
        _rect(bounds, source, f"{path}.screen_bounds", crop=False)
        for axis, length in (("x", "width"), ("y", "height")):
            if (screen[axis] < bounds[axis]
                    or screen[axis] + screen[length] > bounds[axis] + bounds[length]):
                raise AnchorError(f"{path}.screen", "crop exceeds the approved screen bounds")
        result["screen_bounds"] = bounds
    return result


def _layout(item: ReelItem, crops: Mapping[str, Crop], source: ReelSource,
            preset: ReelPreset, settings: ReelCompileSettings, fallback: Rect | None,
            path: str) -> tuple[TimelineLayout, dict[str, dict], float]:
    split = item["layout"] == "split"
    geometry = settings.split_810 or {}
    speaker_height = preset.split_speaker_height_px
    screen_height = preset.split_screen_height_px
    tolerance = preset.screen_fit_tolerance_px
    if split and speaker_height is None:
        speaker_height = geometry.get("split_speaker_height_px")
        screen_height = geometry.get("split_screen_height_px")
        tolerance = geometry.get("screen_fit_tolerance_px")
        if None in (speaker_height, screen_height, tolerance):
            raise AnchorError("settings.split_810", "split band heights and screen fit tolerance are required")
    if split and (min(speaker_height, screen_height) <= 0
                  or (speaker_height | screen_height) % 2 or tolerance < 0
                  or speaker_height + screen_height + preset.divider_px != preset.height):
        raise AnchorError(f"{path}.layout", "split bands must be even and fill the canvas with the divider")

    crop = crops.get(item.get("crop"), {})
    crop_path = f"crops.{item['crop']}" if "crop" in item else f"{path}.crop"
    speaker = crop.get("speaker", fallback)
    if speaker is None:
        raise AnchorError(f"{crop_path}.speaker", "provide an approved speaker crop or a measured medium crop")
    _rect(speaker, source, f"{crop_path}.speaker")
    short = speaker["width"] < preset.width or speaker["height"] < (speaker_height if split else preset.height)
    if short and not preset.allow_speaker_upscale:
        if split or not settings.speaker_background:
            raise AnchorError(f"{crop_path}.speaker", "speaker crop needs upscaling or a planned background fill")
    result: TimelineLayout = {"mode": item["layout"], "speaker": speaker.copy()}
    switches = {}
    filled = not split and short and settings.speaker_background
    if split:
        result.update(_screen(crop, source, crop_path))
        for switch in item["switches"]:
            key = switch["crop"]
            switches[key] = _screen(crops[key], source, f"crops.{key}")
        filled = all(
            abs(Fraction(layout["screen"]["height"] * preset.width, layout["screen"]["width"])
                - screen_height) <= tolerance
            for layout in (result, *switches.values())
        )
    return result, switches, (preset.caption_filled_screen_y_frac if filled else preset.caption_other_y_frac)


def _opening_pause(onset: int, low: int, lead: int, pauses: Sequence[Bounds],
                   preset: ReelPreset) -> Bounds | None:
    for minimum in preset.pause_lengths_ms:
        for start, end in reversed(pauses):
            a, b = max(start, low, onset - lead), min(end, onset)
            if (end - start >= minimum and b - a >= min(lead, preset.audio_open_ms)
                    and onset - b <= preset.pre_word_fallback_ms):
                return a, b
    return None


def _closing_pause(offset: int, high: int, tail: int, fade: int, pauses: Sequence[Bounds],
                   preset: ReelPreset) -> Bounds | None:
    for minimum, window in zip(preset.pause_lengths_ms, preset.tail_pause_windows_ms):
        for start, end in pauses:
            a, b = max(start, offset), min(end, high, offset + window, offset + tail)
            if end - start >= minimum and b - a >= fade:
                return a, b
    return None


def _resolve_piece(span: ResolvedRange, *, path: str, piece_id: str, handles: Bounds,
                   source: ReelSource, words: Sequence[ResolvedWord],
                   word_counts: Mapping[str, int],
                   untimed_cues: Sequence[Cue], cues: Sequence[Cue], phrase: bool,
                   pauses: Sequence[Bounds], preset: ReelPreset, settings: ReelCompileSettings,
                   first: bool, last: bool) -> _Piece:
    start, end = span.start_ms, span.end_ms
    lo, hi = handles
    duration = source.duration_ms
    if source.source_start:
        duration = int(max(source.picture_end_ms, source.sound_end_ms))
    if not 0 <= lo <= start < end <= hi <= duration:
        raise AnchorError(path, "accepted bounds and approved handles must lie inside the recording")
    for edge, label in ((span.start, "from"), (span.end, "through")):
        if edge and edge.provenance == "interpolated":
            raise AnchorError(f"{path}.{label}", "an interpolated word cannot certify a cut")

    selected = tuple(w for w in words if 2 * start <= w.start.ms + w.end.ms < 2 * end)
    selected_ids = {w.id for w in selected}
    selected_cues = tuple(cue for cue in cues if start <= cue.start_ms < cue.end_ms <= end and cue.text.strip())
    untimed_inside = any(2 * start <= c.start_ms + c.end_ms < 2 * end for c in untimed_cues)
    if not phrase and untimed_inside:
        raise AnchorError(path, "word timing is unavailable inside the piece; use captions.mode: local_phrase")
    if phrase and not selected_cues:
        raise AnchorError(path, "whole-phrase fallback requires a complete proofread cue inside the accepted range")
    cue_edges = phrase and (not selected or untimed_inside)
    if cue_edges:
        # A subtitle timestamp alone cannot certify a speech edge.
        onset, offset = selected_cues[0].start_ms, max(c.end_ms for c in selected_cues)
        if any(c.start_ms < end and start < c.end_ms and c not in selected_cues
               for c in untimed_cues):
            raise AnchorError(path, "whole-phrase cut would intersect an unselected cue")
    elif not selected:
        raise AnchorError(path, "no timed words inside the piece")
    else:
        leading, trailing = selected[0].start, max(selected, key=lambda w: w.end.ms).end
        onset, offset = leading.ms, trailing.ms
        if onset < start or offset > end:
            raise AnchorError(path, "accepted range would split a selected word; approve the whole word")
        if leading.provenance != "measured" or trailing.provenance != "measured":
            raise AnchorError(path, "first and last word edges need measured timing")

    prior_words = [w for w in words if w.id not in selected_ids and w.start.ms < onset]
    next_words = [w for w in words if w.id not in selected_ids and w.start.ms >= onset]
    prior_ends = [w.end.ms for w in prior_words]
    next_starts = [w.start.ms for w in next_words]
    prior_ends.extend(c.end_ms for c in untimed_cues if c not in selected_cues and c.start_ms < onset)
    next_starts.extend(c.start_ms for c in untimed_cues if c not in selected_cues and c.start_ms >= onset)
    lo = max([lo, *prior_ends])
    hi = min([hi, *next_starts])
    if not lo <= onset < offset <= hi:
        raise AnchorError(path, "neighboring speech overlaps the selected word or cue boundary")
    if cue_edges and any(not onset <= w.start.ms <= w.end.ms <= offset for w in selected):
        raise AnchorError(path, "whole-phrase fallback would split a timed word")
    if not source.aligned_start_ms <= onset < offset <= source.duration_ms:
        raise AnchorError(path, "selected speech lies outside the shared picture and sound")
    lo = max(lo, ceil(source.aligned_start_ms))

    lead = preset.quiet_lead_ms if first else preset.inner_handle_ms
    tail = preset.tail_picture_ms if last else preset.inner_handle_ms
    opening = _opening_pause(onset, lo, lead, pauses, preset)
    sound_hi = min(hi, int(source.sound_end_ms))
    closing = _closing_pause(offset, sound_hi, tail, settings.audio_close_ms, pauses, preset)
    open_until = onset
    # Use the measured word edge when no quiet pre-roll can be certified.
    if onset == 0:
        open_at = 0
    elif opening:
        open_until = opening[1]
        open_at = open_until - min(lead, preset.audio_open_ms)
    elif cue_edges:
        raise AnchorError(path, "whole-phrase start needs a measured quiet window")
    else:
        # The fallback can start at a known prior word end within the preset look-back.
        prior_word_edge = any(w.end.ms == lo and w.end.provenance == "measured" for w in prior_words)
        open_at = lo if prior_word_edge and onset - lo <= min(lead, preset.pre_word_fallback_ms) else onset
    if first and open_until - open_at < preset.audio_open_ms:
        # If measured quiet is absent, raise gain over the onset while keeping
        # the terminal half of the last word at full gain.
        last_start = (max(selected_cues, key=lambda cue: cue.end_ms).start_ms if cue_edges
                      else max(selected, key=lambda w: w.end.ms).start.ms)
        open_until = min(open_at + preset.audio_open_ms, Fraction(last_start + offset, 2))

    close_from = offset
    if offset == int(source.sound_end_ms) or offset == source.duration_ms:
        close_at = offset
    elif closing:
        close_from = closing[0]
        close_at = close_from + settings.audio_close_ms
    elif not cue_edges and any(
        w.start.ms == sound_hi and w.start.provenance == "measured" for w in next_words
    ) and settings.audio_close_ms <= sound_hi - offset <= min(tail, max(preset.tail_pause_windows_ms)):
        # A short gap bounded by two measured word edges can end at the next onset.
        close_at = sound_hi
    else:
        raise AnchorError(path, "cannot fit the audio close after the last word in a measured quiet window")

    picture_start = max(lo, onset - lead, opening[0] if opening else lo)
    picture_end = min(hi, offset + tail, closing[1] if closing else hi, int(source.picture_end_ms))
    selected_counts = Counter(w.start.cue_id for w in selected)
    share = Fraction(str(preset.cue_word_share))
    caption_cue_ids = frozenset(
        cue_id for cue_id, count in selected_counts.items()
        if span.fragment_id is None or count >= share * word_counts[cue_id]
    )
    return _Piece(piece_id, path, onset, offset, open_at, open_until, close_from, close_at,
                  picture_start, picture_end,
                  lead, tail, preset.picture_fade_in_ms if first else preset.inner_picture_fade_ms,
                  preset.picture_fade_out_ms if last else preset.inner_picture_fade_ms,
                  selected, selected_cues, caption_cue_ids)


@dataclass(frozen=True)
class _PieceClock:
    piece: _Piece
    occurrence: EditOccurrence
    clock: EditClock
    frames_per_ms: Fraction
    samples_per_ms: Fraction

    def sample(self, ms: int | Fraction) -> int:
        return self.occurrence.start_sample + round(
            (self.piece.lead + ms - self.piece.start) * self.samples_per_ms)

    def frame(self, ms: int | Fraction) -> int:
        point = self.occurrence.start_frame + round(
            (self.piece.lead + ms - self.piece.start) * self.frames_per_ms)
        return min(self.occurrence.end_frame, max(self.occurrence.start_frame, point))

    def span(self, start: int, end: int | Fraction) -> OutputSpan:
        return {"frames": {"start": self.frame(start), "end": self.frame(end)},
                "samples": {"start": self.sample(start), "end": self.sample(end)}}


def _piece_element(mapping: _PieceClock, source_id: str, source: ReelSource) -> TimelineElement:
    piece, row = mapping.piece, mapping.occurrence
    if not row.start_sample <= mapping.sample(piece.open_at) < mapping.sample(piece.close_at) <= row.end_sample:
        raise AnchorError(piece.path, "the frame-rounded piece cannot contain the measured sound and its fades")
    native = Fraction(source.media["sample_rate"], 1000)
    frames = {"start": int((piece.picture_start - source.picture_start_ms) * source.frames_per_ms),
              "end": min(source.frames, ceil((piece.picture_end - source.picture_start_ms) * source.frames_per_ms))}
    # A floor at the left and a ceil at the right retain the pictures covering speech.
    first = max(row.start_frame, mapping.frame(piece.picture_start))
    last = min(row.end_frame, mapping.frame(piece.picture_end))
    if first >= last:
        raise AnchorError(piece.path, "piece has no representable source picture")
    events: list[TimelineEvent] = []

    def frames_event(kind, start, end, **extra):
        if start < end:
            events.append({"kind": kind, "output": {"frames": {"start": start, "end": end},
                           "samples": {"start": mapping.clock.sample_at_frame(start),
                                       "end": mapping.clock.sample_at_frame(end)}}, **extra})

    frames_event("freeze", row.start_frame, first, source_frame=frames["start"])
    frames_event("freeze", last, row.end_frame, source_frame=frames["end"] - 1)
    frames_event("picture_fade_in", row.start_frame,
                 min(mapping.frame(piece.start), row.start_frame + round(piece.fade_in * mapping.frames_per_ms)))
    frames_event("picture_fade_out",
                 max(mapping.frame(piece.end), row.end_frame - round(piece.fade_out * mapping.frames_per_ms)),
                 row.end_frame)
    for kind, start, end in (
        ("audio_fade_in", piece.open_at, piece.open_until), ("speech", piece.start, piece.end),
        ("audio_fade_out", piece.close_from, piece.close_at),
    ):
        if start < end:
            events.append({"kind": kind, "output": mapping.span(start, end)})
    for start_frame, end_frame, start_sample, end_sample in (
        (row.start_frame, mapping.frame(piece.open_at), row.start_sample, mapping.sample(piece.open_at)),
        (mapping.frame(piece.close_at), row.end_frame, mapping.sample(piece.close_at), row.end_sample),
    ):
        if start_sample < end_sample:
            events.append({"kind": "silence", "output": {
                "frames": {"start": start_frame, "end": end_frame},
                "samples": {"start": start_sample, "end": end_sample},
            }})
    return {"id": row.id, "kind": "piece", "output": row.output, "rate": {"num": 1, "den": 1},
            "source": {"source_id": source_id, "frames": frames,
                       "samples": {"start": int(piece.open_at * native),
                                   "end": ceil(piece.close_at * native)}},
            "audio": "source", "events": events}


def _captions(mapping: _PieceClock, mode: str, preset: ReelPreset, source: ReelSource
              ) -> tuple[list[TimelineWord], list[TimelineCaption]]:
    piece = mapping.piece
    native = Fraction(source.media["sample_rate"], 1000)
    words = []
    by_cue = {}
    for word in piece.words:
        a, b = word.start.ms, word.end.ms
        if a >= b or not word.text.strip():
            continue
        output = mapping.span(a, b)
        if output["samples"]["start"] == output["samples"]["end"]:
            continue
        matched = word.start.provenance == word.end.provenance == "measured"
        row: TimelineWord = {
            "element_id": piece.id, "cue_id": word.start.cue_id, "word_id": word.id, "text": word.text,
            "source_samples": {"start": int(a * native), "end": ceil(b * native)},
            "output": output, "provenance": "matched" if matched else "interpolated",
        }
        if matched and word.start.source_word_id:
            row["source_word_id"] = word.start.source_word_id
        words.append(row)
        if word.start.cue_id in piece.caption_cue_ids:
            by_cue.setdefault(word.start.cue_id, []).append(word)
    if mode == "local_phrase":
        return words, [
            {"element_id": piece.id, "cue_id": cue.id, "mode": "phrase", "text": cue.text,
             "output": mapping.span(max(cue.start_ms, piece.start), min(cue.end_ms, piece.end)), "word_ids": []}
            for cue in piece.cues if max(cue.start_ms, piece.start) < min(cue.end_ms, piece.end)
        ]

    captions = []
    for cue_id, tokens in by_cue.items():
        times = [(w.text, w.start.ms, w.end.ms) for w in tokens]
        for page in paged(times, preset.caption_page_words, preset.caption_page_chars,
                          preset.caption_pause_break_ms):
            group = [tokens[i] for i in page]
            captions.append({"element_id": piece.id, "cue_id": cue_id, "mode": "word",
                             "text": " ".join(w.text for w in group),
                             "output": mapping.span(group[0].start.ms, max(w.end.ms for w in group)),
                             "word_ids": [w.id for w in group]})
    return words, captions


def _anchor_word_id(resolver: AnchorResolver, anchor: Anchor, path: str,
                    cue_index: Mapping[str, Cue]) -> str:
    if "cue_id" in anchor and "phrase" not in anchor:
        resolved = resolver.resolve_anchor(anchor, path=path)
        cue = cue_index[resolved.cue_id]
        for index, token in enumerate(cue.text.split(), 1):
            if norm(token):
                return f"{cue.id}:w{index}"
        raise AnchorError(path, "cue contains no anchorable words")
    return resolver.resolve_anchor(anchor, path=path, require_measured=True).word_id


def _matched_word(word_id: str, path: str, words: Sequence[TimelineWord]) -> TimelineWord:
    matches = [w for w in words if w["word_id"] == word_id and w["provenance"] == "matched"]
    if len(matches) != 1:
        raise AnchorError(path, "anchor must identify one retained matched word occurrence")
    return matches[0]


def _effect_props(effect, word: TimelineWord, row: EditOccurrence, preset: ReelPreset,
                  settings: ReelCompileSettings, fps: Fraction, path: str) -> dict:
    kind = effect["type"]
    duration = effect.get("duration_ms", settings.default_effect_duration_ms.get(kind))
    _positive(duration, f"{path}.duration_ms (or settings.default_effect_duration_ms.{kind})")
    start = max(row.start_frame, min(
        word["output"]["frames"]["start"] - round(preset.effect_lead_ms * fps),
        row.end_frame - round(preset.effect_latest_start_from_end_ms * fps),
    ))
    end = start + round(duration * fps)
    if not start < end <= row.end_frame:
        raise AnchorError(f"{path}.duration_ms", "effect does not fit inside the retained piece")
    style = settings.effect_styles.get(kind, {})
    props = {"type": kind, "startFrame": start, "endFrame": end}
    if kind == "zoom":
        scale = effect.get("scale", style.get("scale"))
        if scale is None:
            raise AnchorError(f"{path}.scale", "supply an authored or build-policy zoom scale")
        return {**props, "scale": scale}
    if kind == "shake":
        if "amplitudePx" not in style:
            raise AnchorError("settings.effect_styles.shake.amplitudePx", "required for shake")
        return {**props, "amplitudePx": style["amplitudePx"]}
    slot = effect.get("slot", style.get("slot"))
    if slot is None or "box" not in style:
        raise AnchorError(f"settings.effect_styles.{kind}", "resolved box and slot are required")
    props.update(slot=slot, box=style["box"].copy())
    if kind == "tag":
        return {**props, "text": effect["text"]}
    asset = settings.effect_assets.get(effect["asset"])
    if asset is None:
        raise AnchorError(f"{path}.asset", "asset has no approved public-path binding")
    return {**props, "asset": asset}


def compile_reel(
    plan: ReelsPlan, item_id: str, *, accepted: Mapping[str, Bounds], cues: Sequence[Cue],
    word_times: Mapping[str, Sequence[WordTiming]] | None, source_id: str, source: ReelSource,
    pauses: Sequence[Bounds], settings: ReelCompileSettings, accepted_sha256: str,
    transcript_sha256: str, plan_transcript_sha256: str, source_words_sha256: str | None,
    word_times_transcript_sha256: str | None, handle_bounds: Mapping[str, Bounds] | None = None,
    medium_crop: Rect | None = None,
) -> ReelTimeline:
    """Compile one item from parsed YAML and current, locally measured inputs.

    Accepted and handle bounds are source milliseconds for a single recording.
    Handle approvals are keyed by accepted fragment ID and default to its bounds.
    """
    _check(validate_reels(plan))
    path = f"items.{item_id}"
    if item_id not in plan["items"] or item_id not in accepted:
        raise AnchorError(path, "expected a configured item with an accepted source range")
    for value, name in ((settings.sample_rate, "settings.sample_rate"),
                        (settings.audio_close_ms, "settings.audio_close_ms"),
                        (source.width, "source.width"), (source.height, "source.height"),
                        (source.frames, "source.frames"), (source.samples, "source.samples"),
                        (source.media["frame_rate"]["num"], "source.media.frame_rate.num"),
                        (source.media["frame_rate"]["den"], "source.media.frame_rate.den"),
                        (source.media["sample_rate"], "source.media.sample_rate")):
        _positive(value, name)
    if word_times and source_words_sha256 is None:
        raise AnchorError("source_words_sha256", "word times require the current source word digest")

    effective = normalize_reel_item(plan, item_id)
    item = effective["items"][item_id]
    preset_name = effective["preset"]
    preset = REEL_PRESETS[preset_name]
    rate = Fraction(preset.fps)
    fps = rate / 1000
    ratio: Ratio = {"num": rate.numerator, "den": rate.denominator}
    mode = item["captions"]["mode"]
    resolver = AnchorResolver(cues, transcript_sha256=transcript_sha256,
                              plan_transcript_sha256=plan_transcript_sha256, word_times=word_times,
                              word_times_transcript_sha256=word_times_transcript_sha256, fragments=accepted)
    refs = item.get("pieces", [{"fragment": item_id}])
    # Resolve authored anchors before collecting or testing timing evidence.
    ranges = [resolver.resolve_range(ref, path=f"{path}.pieces[{i}]",
                                     envelope=accepted[item_id] if "fragment" not in ref else None)
              for i, ref in enumerate(refs)]
    cue_index = {cue.id: cue for cue in cues}
    anchor_ids = {
        name: [_anchor_word_id(resolver, row["at"], f"{path}.{name}[{i}].at", cue_index)
               for i, row in enumerate(item[name])]
        for name in ("switches", "effects")
    }
    ordered_cues = sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms))
    all_words = []
    untimed = []
    for cue in ordered_cues:
        rows = resolver.words_for_cue(cue.id, path=f"transcript.cues.{cue.id}")
        timed = [w for w in rows if norm(w.text) and w.start.ms < w.end.ms]
        if not timed and cue.text.strip():
            untimed.append(cue)
        all_words.extend(timed)
    all_words.sort(key=lambda word: (word.start.ms, word.end.ms))
    word_counts = Counter(word.start.cue_id for word in all_words)
    layout, screen_switches, caption_y = _layout(
        item, effective["crops"], source, preset, settings, medium_crop, path)
    pieces = []
    duration = int(max(source.picture_end_ms, source.sound_end_ms)) if source.source_start else source.duration_ms
    if any(type(a) is not int or type(b) is not int or not 0 <= a < b for a, b in pauses):
        raise AnchorError("pauses", "quiet windows need nonnegative integer millisecond bounds")
    pauses = sorted((a, min(b, duration)) for a, b in pauses if a < duration)
    for i, span in enumerate(ranges):
        fragment_id = span.fragment_id or item_id
        bounds = (handle_bounds or {}).get(fragment_id, accepted[fragment_id])
        pieces.append(_resolve_piece(
            span, path=f"{path}.pieces[{i}]", piece_id=f"piece-{i + 1}",
            handles=bounds, source=source, words=all_words, word_counts=word_counts,
            untimed_cues=untimed, cues=ordered_cues, phrase=mode == "local_phrase",
            pauses=pauses, preset=preset, settings=settings, first=i == 0, last=i == len(ranges) - 1,
        ))
    cards = item["cards"]
    # Kept spans use each virtual piece clock, including its held picture/silence.
    spans = [KeptSpan(piece.id, 0, piece.duration, piece.id) for piece in pieces]
    if "intro" in cards:
        spans.insert(0, Gap(preset.intro_ms, "intro"))
    spans.append(Gap(preset.thought_ms, "thought"))
    clock = EditClock(spans, frame_rate=ratio, sample_rate=settings.sample_rate)
    elements, words, captions = [], [], []
    by_piece = {p.id: p for p in pieces}
    occurrences = {row.id: row for row in clock.occurrences}
    for row in clock.occurrences:
        if row.id in by_piece:
            mapping = _PieceClock(by_piece[row.id], row, clock, fps, Fraction(settings.sample_rate, 1000))
            elements.append(_piece_element(mapping, source_id, source))
            word_rows, caption_rows = _captions(mapping, mode, preset, source)
            words.extend(word_rows)
            captions.extend(caption_rows)
            continue
        card = {"text": cards[row.id]}
        frames = row.end_frame - row.start_frame
        dwell = settings.thought_readable_dwell_frames
        reveal = 0
        if row.id == "intro":
            dwell = settings.intro_readable_dwell_frames
            count = len(card["text"].split())
            reveals = [round(i * Fraction(str(preset.intro_reveal_fraction)) * frames / count)
                       for i in range(count)]
            card["word_reveal_frames"] = reveals
            reveal = reveals[-1]
        elif "subline" in cards:
            reveal = round(frames * Fraction(str(preset.subline_reveal_fraction)))
            card["subline"] = {"text": cards["subline"], "revealFrame": reveal}
        _positive(dwell, f"settings.{row.id}_readable_dwell_frames")
        if frames - reveal < dwell:
            raise AnchorError(f"{path}.cards.{row.id}", "card cannot meet its required readable dwell")
        elements.append({"id": row.id, "kind": row.id, "output": row.output, "card": card,
                         "template": row.id, "audio": "silent", "rate": {"num": 1, "den": 1}, "events": []})

    captions.sort(key=lambda caption: caption["output"]["samples"]["start"])
    for caption, following in zip(captions, captions[1:]):
        for axis in ("frames", "samples"):
            caption["output"][axis]["end"] = min(caption["output"][axis]["end"],
                                                 following["output"][axis]["start"])
    switches, effects = [], []
    for i, switch in enumerate(item["switches"]):
        where = f"{path}.switches[{i}]"
        word = _matched_word(anchor_ids["switches"][i], f"{where}.at", words)
        frame = word["output"]["frames"]["start"]
        if any(row["output_frame"] == frame for row in switches):
            raise AnchorError(f"{where}.at", "another screen switch already owns this frame")
        switches.append({"word_id": word["word_id"], "element_id": word["element_id"],
                         "output_frame": frame, **screen_switches[switch["crop"]]})
    switches.sort(key=lambda row: row["output_frame"])
    for i, effect in enumerate(item["effects"]):
        where = f"{path}.effects[{i}]"
        word = _matched_word(anchor_ids["effects"][i], f"{where}.at", words)
        row = occurrences[word["element_id"]]
        effects.append({"word_id": word["word_id"], "element_id": row.id,
                        "props": _effect_props(effect, word, row, preset, settings, fps, where)})
    evidence = [COMPILER_REVISION, preset.revision, asdict(source), asdict(settings), pauses,
                layout, elements, words, captions, switches, effects]
    evidence_sha = sha256_obj(evidence)
    timeline: ReelTimeline = {
        "version": 1, "kind": "reel", "item": item_id, "preset": preset_name,
        "preset_revision": preset.revision, "compiler_revision": COMPILER_REVISION,
        "inputs": {"plan": plan_digest(effective), "accepted": accepted_sha256,
                   "transcript": transcript_sha256, "source_words": source_words_sha256,
                   "sources": {source_id: deepcopy(source.media)}, "analysis": {"reel-evidence": evidence_sha}},
        "frame_rate": ratio, "canvas": {"width": preset.width, "height": preset.height},
        "sample_rate": settings.sample_rate, "duration_frames": clock.duration_frames,
        "duration_samples": clock.duration_samples, "elements": elements,
        "video_units": [{"id": e["id"], "output": e["output"], "element_ids": [e["id"]]} for e in elements],
        "layout": layout, "caption_mode": mode, "caption_y_frac": item["captions"].get("y_frac", caption_y),
        "crop_switches": switches, "effects": effects, "words": words, "captions": captions,
    }
    if "thumbnail" in cards:
        timeline["thumbnail"] = cards["thumbnail"]
    if source.source_start:
        timeline["source_start"] = deepcopy(source.source_start)
    speaker = layout["speaker"]
    if (layout["mode"] == "medium" and settings.speaker_background
            and (speaker["width"] < preset.width or speaker["height"] < preset.height)):
        timeline["speaker_background"] = True
    _check(validate_timeline(timeline))
    return timeline
