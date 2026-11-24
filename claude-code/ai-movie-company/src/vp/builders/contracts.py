"""Strict v1 builder wire contracts. Validators accept parsed data, without I/O."""
from copy import deepcopy
from fractions import Fraction as Rational
from functools import cache
import hashlib
import json
import math
import re
from types import UnionType
from typing import (
    Annotated, Literal, NotRequired, TypedDict, Union, get_args, get_origin,
    get_type_hints, is_typeddict,
)

from .presets import DEFAULT_REEL_PRESET, REEL_PRESETS


DEFAULT_LONG_PRESET = "owner_long_v1"


# Primitive constraints are shared by structural and semantic validation.
def _minimum(low):
    return lambda value: None if value >= low else f"must be at least {low}"


def _pattern(pattern, message):
    return lambda value: None if re.fullmatch(pattern, value) else message


def _text(value):
    return None if value.strip() else "must be nonempty text"


def _public_path(value):
    if re.search(r"[\\:?#%\x00-\x1f\x7f]", value) or any(
        part in ("", ".", "..") for part in value.split("/")
    ):
        return "expected a relative path inside the public directory"
    return None


def _copy_rule(limit, words=None):
    def check(value):
        if "\n" in value.strip() or "<" in value or ">" in value:
            return "must be one line without angle brackets"
        if len(value) > limit:
            return f"must be at most {limit} characters"
        if words is not None and len(value.split()) > words:
            return f"must be at most {words} words"
        return None
    return check


Text = Annotated[str, _text]
PublicPath = Annotated[Text, _public_path]
ID = Annotated[str, _pattern(r"[\w-]+", "expected an identifier")]
WordID = Annotated[str, _pattern(
    r"(?!\d+(?:[.,]\d+|:\d+(?::\d+)?(?:[.,]\d+)?)?$)[\w:.-]+",
    "expected an engine word ID, for example 84:w3",
)]
Digest = Annotated[str, _pattern(r"[0-9a-f]{64}", "expected a lowercase SHA-256 digest")]
Count = Annotated[int, _minimum(0)]
Positive = Annotated[int, _minimum(1)]
Fraction = Annotated[float, lambda value: None if 0 <= value <= 1 else "must be between 0 and 1"]
PositiveNumber = Annotated[float, lambda value: None if value > 0 else "must be positive"]
ZoomScale = Annotated[float, _minimum(1)]
IntroCopy = Annotated[Text, _copy_rule(80)]
ThoughtCopy = Annotated[Text, _copy_rule(140)]
ThumbnailCopy = Annotated[Text, _copy_rule(40, 5)]
ReelPresetName = Literal["owner_reel_1080", "owner_reel_810"]
LongPresetName = Literal["owner_long_v1"]
LayoutMode = Literal["medium", "split"]
CaptionMode = Literal["aligned_required", "local_phrase"]
Side = Literal["left", "right"]
TagSlot = Literal["upper", "lower"]


class Rect(TypedDict):
    x: Count
    y: Count
    width: Positive
    height: Positive


class Crop(TypedDict, total=False):
    speaker: Rect
    screen: Rect
    screen_bounds: Rect


class CueAnchor(TypedDict):
    cue_id: ID
    phrase: NotRequired[Text]
    occurrence: NotRequired[Positive]


class WordAnchor(TypedDict):
    word_id: WordID


Anchor = CueAnchor | WordAnchor


class FragmentRange(TypedDict):
    fragment: ID


# Functional syntax retains the editorial spelling of the Python keyword.
AnchoredRange = TypedDict("AnchoredRange", {"from": Anchor, "through": Anchor})
RangeRef = FragmentRange | AnchoredRange


class CropSwitch(TypedDict):
    at: Anchor
    crop: ID


class ReelCards(TypedDict):
    intro: NotRequired[IntroCopy]
    thought: ThoughtCopy
    subline: NotRequired[Text]
    thumbnail: NotRequired[ThumbnailCopy]


class ReelCaptions(TypedDict, total=False):
    mode: CaptionMode
    y_frac: Fraction


class _EffectAnchor(TypedDict):
    at: Anchor
    duration_ms: NotRequired[Positive]


class TagEffect(_EffectAnchor):
    type: Literal["tag"]
    text: Text
    slot: NotRequired[TagSlot]


class StickerEffect(_EffectAnchor):
    type: Literal["sticker"]
    asset: ID
    slot: NotRequired[Side]


class FlybyEffect(_EffectAnchor):
    type: Literal["flyby"]
    asset: ID
    slot: NotRequired[Side]


class ZoomEffect(_EffectAnchor):
    type: Literal["zoom"]
    scale: NotRequired[ZoomScale]


class ShakeEffect(_EffectAnchor):
    type: Literal["shake"]


ReelEffect = TagEffect | StickerEffect | FlybyEffect | ZoomEffect | ShakeEffect


class ReelItem(TypedDict):
    pieces: NotRequired[list[RangeRef]]
    layout: NotRequired[LayoutMode]
    crop: NotRequired[ID]
    switches: NotRequired[list[CropSwitch]]
    cards: ReelCards
    captions: NotRequired[ReelCaptions]
    effects: NotRequired[list[ReelEffect]]


class ReelsPlan(TypedDict):
    version: Literal[1]
    preset: NotRequired[ReelPresetName]
    crops: NotRequired[dict[ID, Crop]]
    items: dict[ID, ReelItem]


class LongCut(TypedDict):
    id: ID
    range: RangeRef
    action: Literal["drop", "shorten_pause", "timelapse"]
    label: NotRequired[Text]


class LongHook(TypedDict):
    range: RangeRef
    title: Text
    subtitle: NotRequired[Text]
    end_text: NotRequired[Text]


class LongOverlay(TypedDict):
    id: ID
    at: Anchor
    text: Text
    duration_ms: NotRequired[Positive]


class LongOutro(TypedDict):
    title: Text
    points: list[Text]
    subline: NotRequired[Text]


class LongPlan(TypedDict):
    version: Literal[1]
    item: ID
    preset: NotRequired[LongPresetName]
    body: NotRequired[RangeRef]
    cuts: NotRequired[list[LongCut]]
    hook: NotRequired[LongHook]
    teaser: NotRequired[list[RangeRef]]
    chapters: NotRequired[list[LongOverlay]]
    subscribe: NotRequired[list[LongOverlay]]
    outro: NotRequired[LongOutro]
    captions: NotRequired[Literal["sidecar"]]


# Remotion wire types. Times are integer frames in the composition clock.
class RenderFrame(TypedDict):
    width: Positive
    height: Positive
    fps: PositiveNumber
    durationInFrames: Positive


class RenderBrand(TypedDict):
    fontFamily: Text
    textColor: Text
    plateColor: Text
    cardBg: Text
    cardTextColor: Text
    accentColor: Text
    channelPlate: Text | None


class RenderBox(TypedDict):
    x: Annotated[float, _minimum(0)]
    y: Annotated[float, _minimum(0)]
    width: PositiveNumber
    height: PositiveNumber


class RenderCaptionStyle(TypedDict):
    fontFrac: PositiveNumber
    yFrac: Fraction
    plate: bool


class RenderCaptionWord(TypedDict):
    text: Text
    startFrame: Count
    endFrame: Count


class RenderCaptionPage(TypedDict):
    text: Text
    mode: Literal["word", "phrase"]
    startFrame: Count
    endFrame: Positive
    words: list[RenderCaptionWord]


class _RenderEffectSpan(TypedDict):
    startFrame: Count
    endFrame: Positive


class RenderTag(_RenderEffectSpan):
    type: Literal["tag"]
    text: Text
    slot: TagSlot
    box: RenderBox


class RenderSticker(_RenderEffectSpan):
    type: Literal["sticker"]
    asset: PublicPath
    slot: Side
    box: RenderBox


class RenderFlyby(_RenderEffectSpan):
    type: Literal["flyby"]
    asset: PublicPath
    slot: Side
    box: RenderBox


class RenderZoom(_RenderEffectSpan):
    type: Literal["zoom"]
    scale: ZoomScale


class RenderShake(_RenderEffectSpan):
    type: Literal["shake"]
    amplitudePx: PositiveNumber


RenderEffect = RenderTag | RenderSticker | RenderFlyby | RenderZoom | RenderShake


class RenderCardPoint(TypedDict):
    text: Text
    revealFrame: Count


class RenderReelBody(RenderFrame):
    kind: Literal["reel_body"]
    video: PublicPath
    brand: RenderBrand
    showPlate: bool
    layout: LayoutMode
    captions: list[RenderCaptionPage]
    captionStyle: RenderCaptionStyle
    effects: list[RenderEffect]


class RenderReelCard(RenderFrame):
    kind: Literal["reel_card"]
    role: Literal["intro", "thought"]
    text: Text
    wordRevealFrames: list[Count]
    subline: NotRequired[RenderCardPoint]
    brand: RenderBrand
    showPlate: bool


class RenderOverlay(TypedDict):
    id: ID
    kind: Literal["chapter", "subscribe"]
    text: Text
    startFrame: int
    endFrame: int
    box: RenderBox


class RenderLongOverlay(RenderFrame):
    kind: Literal["long_overlay"]
    video: PublicPath | None
    brand: RenderBrand
    overlays: list[RenderOverlay]


class RenderLongCard(RenderFrame):
    kind: Literal["long_card"]
    role: Literal["hook", "outro", "timelapse"]
    video: PublicPath | None
    title: Text
    subtitle: NotRequired[Text]
    endText: NotRequired[RenderCardPoint]
    points: list[RenderCardPoint]
    subline: NotRequired[RenderCardPoint]
    brand: RenderBrand


RendererProps = RenderReelBody | RenderReelCard | RenderLongOverlay | RenderLongCard


# Compiled timeline. Frame and sample spans are half-open.
class Span(TypedDict):
    start: Count
    end: Count


class OutputSpan(TypedDict):
    frames: Span
    samples: Span


class Ratio(TypedDict):
    num: Positive
    den: Positive


class PresentationTime(TypedDict):
    num: Count
    den: Positive


class ReelSourceStart(TypedDict):
    picture: PresentationTime
    sound: PresentationTime
    aligned: PresentationTime


class Canvas(TypedDict):
    width: Positive
    height: Positive


class MediaInput(TypedDict):
    sha256: Digest
    frame_rate: Ratio
    sample_rate: Positive


class TimelineInputs(TypedDict):
    plan: Digest
    accepted: Digest
    transcript: Digest
    source_words: Digest | None
    sources: dict[ID, MediaInput]
    design_texts: NotRequired[Digest]
    analysis: NotRequired[dict[ID, Digest]]


class SourceSpan(TypedDict):
    source_id: ID
    samples: Span
    frames: NotRequired[Span]


class TimelineEvent(TypedDict):
    kind: Literal["speech", "freeze", "black", "silence", "picture_fade_in",
                  "picture_fade_out", "audio_fade_in", "audio_fade_out"]
    output: OutputSpan
    source_frame: NotRequired[Count]


class Transition(TypedDict):
    kind: Literal["flash", "picture"]
    output: OutputSpan


class TimelineCard(TypedDict):
    text: Text
    subtitle: NotRequired[Text]
    end_text: NotRequired[RenderCardPoint]
    points: NotRequired[list[RenderCardPoint]]
    subline: NotRequired[RenderCardPoint]
    word_reveal_frames: NotRequired[list[Count]]


class TimelineElement(TypedDict):
    id: ID
    kind: Literal["intro", "piece", "thought", "hook", "teaser", "body", "timelapse", "outro"]
    output: OutputSpan
    source: NotRequired[SourceSpan]
    template: NotRequired[ID]
    card: NotRequired[TimelineCard]
    rate: Ratio
    transition: NotRequired[Transition]
    audio: Literal["source", "silent"]
    events: list[TimelineEvent]


class VideoUnit(TypedDict):
    id: ID
    output: OutputSpan
    element_ids: list[ID]


class TimelineWord(TypedDict):
    element_id: ID
    cue_id: ID
    word_id: WordID
    text: Text
    provenance: Literal["matched", "interpolated"]
    source_samples: Span
    output: OutputSpan
    source_word_id: NotRequired[WordID]


class TimelineCaption(TypedDict):
    element_id: ID
    cue_id: ID
    mode: Literal["word", "phrase"]
    text: Text
    output: OutputSpan
    word_ids: list[WordID]


class TimelineLayout(TypedDict):
    mode: LayoutMode
    speaker: Rect
    screen: NotRequired[Rect]
    screen_bounds: NotRequired[Rect]


class TimelineCropSwitch(TypedDict):
    element_id: ID
    word_id: WordID
    output_frame: Count
    screen: Rect
    screen_bounds: NotRequired[Rect]


class TimelineEffect(TypedDict):
    element_id: ID
    word_id: WordID
    props: RenderEffect


class TimelineOverlay(TypedDict):
    element_id: ID
    output: OutputSpan
    id: ID
    kind: Literal["chapter", "subscribe"]
    text: Text
    cue_id: ID
    word_id: NotRequired[WordID]


class CutResolution(TypedDict):
    id: ID
    action: Literal["drop", "shorten_pause", "timelapse"]
    proposed: SourceSpan
    actual: SourceSpan | None
    start_shift_samples: int | None
    end_shift_samples: int | None
    reason: Text
    evidence: list[Text]


class _Timeline(TypedDict):
    version: Literal[1]
    item: ID
    preset_revision: Text
    compiler_revision: Text
    inputs: TimelineInputs
    frame_rate: Ratio
    canvas: Canvas
    sample_rate: Positive
    duration_frames: Positive
    duration_samples: Positive
    elements: list[TimelineElement]
    video_units: list[VideoUnit]
    words: list[TimelineWord]
    captions: list[TimelineCaption]


class ReelTimeline(_Timeline):
    kind: Literal["reel"]
    preset: ReelPresetName
    layout: TimelineLayout
    caption_mode: CaptionMode
    caption_y_frac: Fraction
    crop_switches: list[TimelineCropSwitch]
    effects: list[TimelineEffect]
    source_start: NotRequired[ReelSourceStart]
    speaker_background: NotRequired[Literal[True]]
    thumbnail: NotRequired[ThumbnailCopy]


class LongTimeline(_Timeline):
    kind: Literal["long"]
    preset: LongPresetName
    cuts: list[CutResolution]
    overlays: list[TimelineOverlay]


Timeline = ReelTimeline | LongTimeline


class _JSONValue:
    """Marker for an open JSON value inside the explicitly named QA values map."""


class QACheck(TypedDict):
    status: Literal["passed", "failed", "not_run"]
    values: dict[ID, _JSONValue]
    evidence: list[Text]
    reason: NotRequired[Text]


class QARecord(TypedDict):
    version: Literal[1]
    item: ID
    kind: Literal["reel", "long"]
    timeline_sha256: Digest
    media_sha256: Digest | None
    policy_revision: Text
    checks: dict[ID, QACheck]


@cache
def _fields(schema):
    hints = get_type_hints(schema, include_extras=True)
    return {
        key: (get_args(hint)[0] if get_origin(hint) is NotRequired else hint,
              key in schema.__required_keys__)
        for key, hint in hints.items()
    }


def _path(path, key):
    if type(key) is str and re.fullmatch(r"[\w-]+", key):
        return f"{path}.{key}" if path else key
    if type(key) is str or (type(key) is int and _number(key)):
        return f"{path}[{key!r}]"
    return f"{path}[invalid key]"


def _add(errors, path, message):
    errors.append(f"{path or '$'}: {message}")


def _number(value):
    return type(value) is float and math.isfinite(value) or (
        type(value) is int and abs(value) <= 2**53 - 1
    )


def _json_errors(value, path, errors, ancestors):
    if value is None or type(value) in (bool, str) or _number(value):
        return
    if type(value) not in (dict, list):
        _add(errors, path, "expected a JSON value")
        return
    if id(value) in ancestors:
        _add(errors, path, "cyclic value is not JSON")
        return
    ancestors.add(id(value))
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is str:
                _json_errors(child, _path(path, key), errors, ancestors)
            else:
                _add(errors, _path(path, key), "expected a string key")
    else:
        for i, child in enumerate(value):
            _json_errors(child, f"{path}[{i}]", errors, ancestors)
    ancestors.remove(id(value))


def _union_errors(variants, value, path):
    if type(value) is dict:
        # Prefer the variant with the matching wire tag or the most recognized fields.
        variants = sorted(variants, key=lambda variant: _union_rank(variant, value), reverse=True)
    attempts = []
    for variant in variants:
        errors = []
        _check(variant, value, path, errors)
        if not errors:
            return []
        attempts.append(errors)
    if type(value) is not dict and all(is_typeddict(variant) for variant in variants):
        return [f"{path or '$'}: expected a mapping"]
    if type(value) is dict and is_typeddict(variants[0]):
        return attempts[0]
    return min(attempts, key=len)


def _union_rank(variant, value):
    if not is_typeddict(variant):
        return (0, 0, 0)
    fields = _fields(variant)
    tags = sum(
        get_origin(hint) is Literal and key in value
        and any(type(value[key]) is type(tag) and value[key] == tag for tag in get_args(hint))
        for key, (hint, _) in fields.items() if key in ("kind", "type")
    )
    return (1, tags, len(fields.keys() & value.keys()))


def _check(schema, value, path, errors):
    origin = get_origin(schema)
    if origin is Annotated:
        base, *rules = get_args(schema)
        before = len(errors)
        _check(base, value, path, errors)
        if len(errors) == before:
            for rule in rules:
                problem = rule(value)
                if problem:
                    _add(errors, path, problem)
    elif origin in (UnionType, Union):
        errors.extend(_union_errors(get_args(schema), value, path))
    elif origin is Literal:
        choices = get_args(schema)
        if not any(type(value) is type(choice) and value == choice for choice in choices):
            _add(errors, path, "expected " + " | ".join(repr(choice) for choice in choices))
    elif is_typeddict(schema):
        if type(value) is not dict:
            _add(errors, path, "expected a mapping")
            return
        fields = _fields(schema)
        for key in value:
            if type(key) is not str or key not in fields:
                _add(errors, _path(path, key), "unknown key")
        for key, (hint, required) in fields.items():
            child = _path(path, key)
            if key in value:
                _check(hint, value[key], child, errors)
            elif required:
                _add(errors, child, "required")
    elif origin is dict:
        if type(value) is not dict:
            _add(errors, path, "expected a mapping")
            return
        key_type, value_type = get_args(schema)
        for key, val in value.items():
            child = _path(path, key)
            _check(key_type, key, child, errors)
            _check(value_type, val, child, errors)
    elif origin is list:
        if type(value) is not list:
            _add(errors, path, "expected a list")
            return
        item_type, = get_args(schema)
        for i, item in enumerate(value):
            _check(item_type, item, f"{path}[{i}]", errors)
    elif schema is _JSONValue:
        _json_errors(value, path, errors, set())
    elif schema is int:
        if type(value) is not int or not _number(value):
            _add(errors, path, "expected a JSON-safe integer")
    elif schema is float:
        if not _number(value):
            _add(errors, path, "expected a finite JSON number")
    elif type(value) is not schema:
        _add(errors, path, "expected " + {str: "text", bool: "a boolean", type(None): "null"}[schema])


def _validate(schema, value, path=""):
    errors = []
    try:
        _check(schema, value, path, errors)
    except RecursionError:
        _add(errors, path, "input nesting is too deep")
    return errors


def _normalized_word(word):
    # Same spelling rule as vp.analysis.align_words.norm.
    return re.sub(r"[\W_]+", "", word.lower().replace("ё", "е"))


def _anchor_errors(anchor, path, errors, cue_words):
    if "word_id" in anchor:
        return
    phrase = anchor.get("phrase")
    if "occurrence" in anchor and phrase is None:
        _add(errors, f"{path}.occurrence", "requires a phrase")
    query = [word for token in phrase.split() if (word := _normalized_word(token))] if phrase else []
    if phrase and not query:
        _add(errors, f"{path}.phrase", "expected at least one transcript word")
    if cue_words is None:
        return
    cue = anchor["cue_id"]
    if type(cue_words) is not dict:
        _add(errors, path, "expected a cue ID to token list mapping")
        return
    if cue not in cue_words:
        _add(errors, f"{path}.cue_id", f"unknown cue {cue}")
    elif query:
        words = cue_words[cue]
        if type(words) not in (list, tuple) or any(type(word) is not str for word in words):
            _add(errors, path, f"invalid transcript tokens for cue {cue}")
            return
        tokens = [word for token in words if (word := _normalized_word(token))]
        matches = sum(tokens[i:i + len(query)] == query for i in range(len(tokens) - len(query) + 1))
        if matches == 0:
            _add(errors, path, f"phrase does not occur in cue {cue}")
        elif "occurrence" not in anchor and matches > 1:
            _add(errors, path, f"phrase is not unique in cue {cue}, give occurrence")
        elif anchor.get("occurrence", 1) > matches:
            _add(errors, f"{path}.occurrence", f"cue {cue} has only {matches} matching occurrence(s)")


def _range_errors(value, path, errors, cue_words):
    for edge in ("from", "through"):
        if edge in value:
            _anchor_errors(value[edge], f"{path}.{edge}", errors, cue_words)


CueWords = dict[str, list[str] | tuple[str, ...]]


def validate_anchor(value: object, *, cue_words: CueWords | None = None) -> list[str]:
    """Optionally verify cue/phrase references using current cue ID -> proofread token list."""
    errors = _validate(Anchor, value)
    if not errors:
        _anchor_errors(value, "$", errors, cue_words)
    return errors


def validate_range_ref(value: object, *, cue_words: CueWords | None = None) -> list[str]:
    errors = _validate(RangeRef, value)
    if not errors:
        _range_errors(value, "$", errors, cue_words)
    return errors


def _split_crop(crop, path, errors):
    for part in ("speaker", "screen"):
        if part not in crop:
            _add(errors, f"{path}.{part}", "required for split layout")


def _unique(seen, value, path, errors):
    if value in seen:
        _add(errors, path, f"duplicate {value!r}")
    seen.add(value)


def validate_reels(value: object, *, cue_words: CueWords | None = None) -> list[str]:
    """Check a parsed reels.yaml and optional transcript text. Crop checks here are shape-only."""
    errors = _validate(ReelsPlan, value)
    if errors:
        return errors
    preset = REEL_PRESETS[value.get("preset", DEFAULT_REEL_PRESET)]
    crops = value.get("crops", {})
    for item_id, item in value["items"].items():
        path = f"items.{item_id}"
        split = item.get("layout", "medium") == "split"
        crop_id = item.get("crop")
        if crop_id is not None and crop_id not in crops:
            _add(errors, f"{path}.crop", f"unknown crop {crop_id}")
        elif split:
            if crop_id is None:
                _add(errors, f"{path}.crop", "required for split layout")
            else:
                _split_crop(crops[crop_id], f"crops.{crop_id}", errors)
        if "pieces" in item and not item["pieces"]:
            _add(errors, f"{path}.pieces", "expected at least one piece")
        for i, piece in enumerate(item.get("pieces", [])):
            _range_errors(piece, f"{path}.pieces[{i}]", errors, cue_words)
        for i, switch in enumerate(item.get("switches", [])):
            at = f"{path}.switches[{i}]"
            _anchor_errors(switch["at"], f"{at}.at", errors, cue_words)
            crop = crops.get(switch["crop"])
            if not split:
                _add(errors, f"{at}.crop", "screen switches require split layout")
            elif crop is None:
                _add(errors, f"{at}.crop", f"unknown crop {switch['crop']}")
            elif "screen" not in crop:
                _add(errors, f"{at}.crop", "switch crop must have a screen rectangle")
        types = set()
        for i, effect in enumerate(item.get("effects", [])):
            at = f"{path}.effects[{i}]"
            _unique(types, effect["type"], f"{at}.type", errors)
            _anchor_errors(effect["at"], f"{at}.at", errors, cue_words)
            if split and effect["type"] == "zoom" and effect.get("scale", 1) > preset.split_zoom_scale:
                _add(errors, f"{at}.scale", f"split zoom is capped at {preset.split_zoom_scale}")
    return errors


def validate_long_plan(value: object, *, cue_words: CueWords | None = None) -> list[str]:
    """Check a parsed long-plan.yaml. Accepted item kinds are verified by the compiler."""
    errors = _validate(LongPlan, value)
    if errors:
        return errors
    if "body" in value:
        _range_errors(value["body"], "body", errors, cue_words)
    if "hook" in value:
        _range_errors(value["hook"]["range"], "hook.range", errors, cue_words)
    for i, ref in enumerate(value.get("teaser", [])):
        _range_errors(ref, f"teaser[{i}]", errors, cue_words)
    seen = set()
    for i, cut in enumerate(value.get("cuts", [])):
        path = f"cuts[{i}]"
        _unique(seen, cut["id"], f"{path}.id", errors)
        _range_errors(cut["range"], f"{path}.range", errors, cue_words)
        if cut["action"] == "timelapse" and "label" not in cut:
            _add(errors, f"{path}.label", "required for timelapse")
    seen = set()
    for key in ("chapters", "subscribe"):
        for i, overlay in enumerate(value.get(key, [])):
            path = f"{key}[{i}]"
            _unique(seen, overlay["id"], f"{path}.id", errors)
            _anchor_errors(overlay["at"], f"{path}.at", errors, cue_words)
    if "outro" in value and len(value["outro"]["points"]) > 4:
        _add(errors, "outro.points", "expected at most four points")
    return errors


def normalize_reels(value: ReelsPlan) -> ReelsPlan:
    """Copy a validated plan and expand static defaults, leaving context-dependent fields absent."""
    # YAML aliases can share effect objects across items with different layout defaults.
    out: ReelsPlan = {
        "version": value["version"], "crops": deepcopy(value.get("crops", {})),
        "items": {key: deepcopy(item) for key, item in value["items"].items()},
    }
    if "preset" in value:
        out["preset"] = value["preset"]
    preset = REEL_PRESETS[out.setdefault("preset", DEFAULT_REEL_PRESET)]
    for item in out["items"].values():
        layout = item.setdefault("layout", "medium")
        item.setdefault("switches", [])
        captions = item.setdefault("captions", {})
        captions.setdefault("mode", "aligned_required")
        if "y_frac" in captions:
            captions["y_frac"] = float(captions["y_frac"]) or 0.0
        for effect in item.setdefault("effects", []):
            if effect["type"] == "zoom":
                if layout == "split":
                    effect.setdefault("scale", preset.split_zoom_scale)
                if "scale" in effect:
                    effect["scale"] = float(effect["scale"])
    return out


def normalize_reel_item(value: ReelsPlan, item_id: str) -> ReelsPlan:
    """Normalize one validated item and its referenced crops for the per-reel cache key."""
    item = value["items"][item_id]
    crop_ids = {switch["crop"] for switch in item.get("switches", [])}
    if "crop" in item:
        crop_ids.add(item["crop"])
    crops = {key: value["crops"][key] for key in sorted(crop_ids)}
    return normalize_reels({
        "version": value["version"], "preset": value.get("preset", DEFAULT_REEL_PRESET),
        "items": {item_id: item}, "crops": crops,
    })


def normalize_long_plan(value: LongPlan) -> LongPlan:
    """Copy a validated long plan and expand static defaults."""
    out = deepcopy(value)
    out.setdefault("preset", DEFAULT_LONG_PRESET)
    out.setdefault("captions", "sidecar")
    for key in ("cuts", "teaser", "chapters", "subscribe"):
        out.setdefault(key, [])
    for overlay in out["chapters"] + out["subscribe"]:
        overlay.setdefault("duration_ms", 4000)
    return out


def plan_digest(normalized: ReelsPlan | LongPlan) -> str:
    """SHA-256 of compact, sorted-key UTF-8 JSON for a normalized plan."""
    payload = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _interval(start, end, limit, path, errors, *, lo=0, allow_empty=False):
    if start < lo:
        _add(errors, f"{path}.startFrame", f"must be at least {lo}")
    if end < start or (end == start and not allow_empty):
        _add(errors, f"{path}.endFrame", "must be after startFrame" if not allow_empty
             else "must be at or after startFrame")
    if end > limit:
        _add(errors, f"{path}.endFrame", f"must be at most {limit}")


def _box_errors(box, width, height, path, errors):
    if box["x"] + box["width"] > width:
        _add(errors, f"{path}.width", "box extends past canvas width")
    if box["y"] + box["height"] > height:
        _add(errors, f"{path}.height", "box extends past canvas height")


def _reveal_errors(frames, duration, path, errors):
    prior = 0
    for i, frame in enumerate(frames):
        if not prior <= frame < duration:
            _add(errors, f"{path}[{i}]", "reveal frames must be ordered and inside the card")
        prior = frame


def validate_renderer_props(value: object) -> list[str]:
    """Check the closed wire shape and composition-local frame bounds."""
    errors = _validate(RendererProps, value)
    if errors:
        return errors
    duration = value["durationInFrames"]
    width, height = value["width"], value["height"]
    kind = value["kind"]
    if kind == "reel_body":
        previous_end = 0
        for i, page in enumerate(value["captions"]):
            path = f"captions[{i}]"
            start, end = page["startFrame"], page["endFrame"]
            _interval(start, end, duration, path, errors, lo=previous_end)
            previous_end = end
            if bool(page["words"]) != (page["mode"] == "word"):
                _add(errors, f"{path}.words", "word mode requires words and phrase mode requires []")
            word_start = start
            for j, word in enumerate(page["words"]):
                _interval(word["startFrame"], word["endFrame"], end, f"{path}.words[{j}]",
                          errors, lo=word_start, allow_empty=True)
                word_start = word["startFrame"]
        seen = set()
        for i, effect in enumerate(value["effects"]):
            path = f"effects[{i}]"
            _unique(seen, effect["type"], f"{path}.type", errors)
            _interval(effect["startFrame"], effect["endFrame"], duration, path, errors)
            if "box" in effect:
                _box_errors(effect["box"], width, height, f"{path}.box", errors)
            if effect["type"] == "zoom" and value["layout"] == "split":
                cap = REEL_PRESETS[DEFAULT_REEL_PRESET].split_zoom_scale
                if effect["scale"] > cap:
                    _add(errors, f"{path}.scale", f"split zoom is capped at {cap}")
    elif kind == "long_overlay":
        seen = set()
        for i, overlay in enumerate(value["overlays"]):
            path = f"overlays[{i}]"
            _unique(seen, overlay["id"], f"{path}.id", errors)
            start, end = overlay["startFrame"], overlay["endFrame"]
            if not start < end or not (start < duration and end > 0):
                _add(errors, f"{path}.endFrame", "overlay must have a positive interval intersecting the chunk")
            _box_errors(overlay["box"], width, height, f"{path}.box", errors)
    elif kind == "reel_card":
        reveals = value["wordRevealFrames"]
        if value["role"] == "intro" or reveals:
            if len(reveals) != len(value["text"].split()):
                _add(errors, "wordRevealFrames", "expected one frame per word")
        _reveal_errors(reveals, duration, "wordRevealFrames", errors)
        if "subline" in value and value["subline"]["revealFrame"] >= duration:
            _add(errors, "subline.revealFrame", "must be inside the card")
    elif kind == "long_card":
        if len(value["points"]) > 4:
            _add(errors, "points", "expected at most four points")
        for key in ("endText", "subline"):
            if key in value and value[key]["revealFrame"] >= duration:
                _add(errors, f"{key}.revealFrame", "must be inside the card")
        previous = 0
        for i, point in enumerate(value["points"]):
            frame = point["revealFrame"]
            if not previous <= frame < duration:
                _add(errors, f"points[{i}].revealFrame", "reveal frames must be ordered and inside the card")
            previous = frame
    return errors


def _span_errors(span, path, errors, *, limit=None, allow_empty=False):
    start, end = span["start"], span["end"]
    if end < start or (end == start and not allow_empty):
        _add(errors, f"{path}.end", "must be after start" if not allow_empty
             else "must be at or after start")
    if limit is not None:
        if start < limit["start"]:
            _add(errors, f"{path}.start", "starts before the containing span")
        if end > limit["end"]:
            _add(errors, f"{path}.end", "ends after the containing span")


def _output_errors(span, path, errors, *, limit, allow_empty_frames=False,
                   allow_empty_samples=False):
    _span_errors(span["frames"], f"{path}.frames", errors, limit=limit["frames"],
                 allow_empty=allow_empty_frames)
    _span_errors(span["samples"], f"{path}.samples", errors, limit=limit["samples"],
                 allow_empty=allow_empty_samples)


def _source_errors(source, sources, path, errors):
    if source["source_id"] not in sources:
        _add(errors, f"{path}.source_id", "unknown media source")
    for clock in ("frames", "samples"):
        if clock in source:
            _span_errors(source[clock], f"{path}.{clock}", errors)


def _partition(rows, clock, bounds, path, errors):
    end = 0
    for i, row in enumerate(rows):
        output = row["output"]
        at = f"{path}[{i}].output"
        _output_errors(output, at, errors, limit=bounds)
        frames, samples = output["frames"], output["samples"]
        if frames["start"] != end:
            _add(errors, f"{at}.frames.start", f"expected contiguous start at frame {end}")
        for edge in ("start", "end"):
            sample = round(frames[edge] * clock)
            if samples[edge] != sample:
                _add(errors, f"{at}.samples.{edge}", f"expected frame boundary at sample {sample}")
        end = frames["end"]
    if end != bounds["frames"]["end"]:
        _add(errors, path, "must cover the full output duration")


def _timeline_elements(value, bounds, clock, errors):
    reel = value["kind"] == "reel"
    kinds = {"intro", "piece", "thought"} if reel else {"hook", "teaser", "body", "timelapse", "outro"}
    source_kinds = {"piece", "hook", "teaser", "body", "timelapse"}
    card_kinds = {"intro", "thought", "hook", "timelapse", "outro"}
    elements = {}
    for i, element in enumerate(value["elements"]):
        path = f"elements[{i}]"
        kind = element["kind"]
        if element["id"] in elements:
            _add(errors, f"{path}.id", f"duplicate {element['id']!r}")
        elements[element["id"]] = element
        if kind not in kinds:
            _add(errors, f"{path}.kind", f"not an element kind for a {value['kind']} timeline")
        if ("source" in element) != (kind in source_kinds):
            _add(errors, f"{path}.source", "source span required for source footage only")
        if "source" in element:
            _source_errors(element["source"], value["inputs"]["sources"], f"{path}.source", errors)
        if ("template" in element) != (kind in card_kinds):
            _add(errors, f"{path}.template", "template ID required for card elements only")
        if ("card" in element) != (kind in card_kinds):
            _add(errors, f"{path}.card", "resolved copy required for card elements only")
        if kind in {"timelapse", "intro", "thought", "outro"} and element["audio"] != "silent":
            _add(errors, f"{path}.audio", "must be silent")
        output = element["output"]
        if "card" in element:
            card = element["card"]
            duration = output["frames"]["end"] - output["frames"]["start"]
            reveals = card.get("word_reveal_frames", [])
            if kind == "intro" or reveals:
                if len(reveals) != len(card["text"].split()):
                    _add(errors, f"{path}.card.word_reveal_frames", "expected one reveal frame per word")
            _reveal_errors(reveals, duration, f"{path}.card.word_reveal_frames", errors)
            for key in ("subline", "end_text"):
                if key in card and card[key]["revealFrame"] >= duration:
                    _add(errors, f"{path}.card.{key}.revealFrame", "must be inside the card")
            points = card.get("points", [])
            if len(points) > 4:
                _add(errors, f"{path}.card.points", "expected at most four points")
            previous = 0
            for j, point in enumerate(points):
                frame = point["revealFrame"]
                if not previous <= frame < duration:
                    _add(errors, f"{path}.card.points[{j}].revealFrame",
                         "reveal frames must be ordered and inside the card")
                previous = frame
        if "transition" in element:
            _output_errors(element["transition"]["output"], f"{path}.transition.output", errors,
                           limit=output, allow_empty_samples=True)
        for j, event in enumerate(element["events"]):
            at = f"{path}.events[{j}]"
            if event["kind"] == "speech" and element["audio"] == "silent":
                _add(errors, f"{at}.kind", "speech needs source audio")
            if (event["kind"] == "freeze") != ("source_frame" in event):
                _add(errors, f"{at}.source_frame", "required for freeze events only")
            _output_errors(event["output"], f"{at}.output", errors, limit=output, allow_empty_frames=True)
    _partition(value["elements"], clock, bounds, "elements", errors)
    sequence = [element["kind"] for element in value["elements"]]
    if reel:
        body = sequence[1:] if sequence[:1] == ["intro"] else sequence
        if len(body) < 2 or body[-1] != "thought" or any(kind != "piece" for kind in body[:-1]):
            _add(errors, "elements", "expected optional intro, one or more pieces, then one thought")
    else:
        section = {"hook": 0, "teaser": 1, "body": 2, "timelapse": 2, "outro": 3}
        if ("body" not in sequence or sequence.count("hook") > 1 or sequence.count("outro") > 1
                or sequence != sorted(sequence, key=lambda kind: section.get(kind, -1))):
            _add(errors, "elements", "expected optional hook, teasers, retained body and inserts, optional outro")
    return elements


def _timeline_words(value, elements, errors):
    words = {}
    for i, word in enumerate(value["words"]):
        path = f"words[{i}]"
        key = word["element_id"], word["word_id"]
        if key in words:
            _add(errors, f"{path}.word_id", "duplicate word in this occurrence")
        words[key] = word
        if value["inputs"]["source_words"] is None:
            _add(errors, f"{path}.provenance", "requires a source word input digest")
        if word["provenance"] == "interpolated" and "source_word_id" in word:
            _add(errors, f"{path}.source_word_id", "interpolated word has no matched source word")
        element = elements.get(word["element_id"])
        if not element or element["audio"] != "source":
            _add(errors, f"{path}.element_id", "expected a source audio occurrence")
            continue
        _output_errors(word["output"], f"{path}.output", errors, limit=element["output"], allow_empty_frames=True)
        source = element.get("source")
        _span_errors(word["source_samples"], f"{path}.source_samples", errors,
                     limit=source["samples"] if source else None)
    previous_end = {"frames": 0, "samples": 0}
    for i, caption in enumerate(value["captions"]):
        path = f"captions[{i}]"
        if value["kind"] == "reel":
            mode = "word" if value["caption_mode"] == "aligned_required" else "phrase"
            if caption["mode"] != mode:
                _add(errors, f"{path}.mode", f"expected {mode!r} for the reel caption mode")
        element = elements.get(caption["element_id"])
        if not element or element["audio"] != "source":
            _add(errors, f"{path}.element_id", "expected a source audio occurrence")
            continue
        output = caption["output"]
        _output_errors(output, f"{path}.output", errors, limit=element["output"],
                       allow_empty_frames=value["kind"] == "long")
        for axis in ("frames", "samples"):
            if output[axis]["start"] < previous_end[axis]:
                _add(errors, f"{path}.output.{axis}.start", "captions must be ordered and non-overlapping")
            previous_end[axis] = max(previous_end[axis], output[axis]["end"])
        if bool(caption["word_ids"]) != (caption["mode"] == "word"):
            _add(errors, f"{path}.word_ids", "word mode requires words and phrase mode requires []")
        seen = set()
        for j, word_id in enumerate(caption["word_ids"]):
            at = f"{path}.word_ids[{j}]"
            _unique(seen, word_id, at, errors)
            word = words.get((caption["element_id"], word_id))
            if not word or word["cue_id"] != caption["cue_id"]:
                _add(errors, at, "expected a word from this cue and occurrence")
    return words


def _matched_anchor(row, kind, path, words, elements, errors):
    element = elements.get(row["element_id"])
    if not element or element["kind"] != kind:
        _add(errors, f"{path}.element_id", f"expected a {kind} occurrence")
    word = words.get((row["element_id"], row["word_id"]))
    if not word or word["provenance"] != "matched":
        _add(errors, f"{path}.word_id", "expected a matched word from this occurrence")
    return element


def _reel_timeline_errors(value, elements, words, errors):
    if starts := value.get("source_start"):
        picture, sound, aligned = (Rational(starts[key]["num"], starts[key]["den"])
                                   for key in ("picture", "sound", "aligned"))
        if aligned != max(picture, sound):
            _add(errors, "source_start.aligned", "must equal the later picture or sound start")
    split = value["layout"]["mode"] == "split"
    if split:
        _split_crop(value["layout"], "layout", errors)
        if "speaker_background" in value:
            _add(errors, "speaker_background", "only applies to a medium shot")
    else:
        canvas, speaker = value["canvas"], value["layout"]["speaker"]
        needs_fill = any(speaker[axis] < canvas[axis] for axis in ("width", "height"))
        if value.get("speaker_background", False) != needs_fill:
            _add(errors, "speaker_background", "required exactly when the speaker crop cannot cover the canvas")
    for i, row in enumerate(value["crop_switches"]):
        path = f"crop_switches[{i}]"
        if not split:
            _add(errors, path, "screen switches require split layout")
        element = _matched_anchor(row, "piece", path, words, elements, errors)
        if element:
            window = element["output"]["frames"]
            if not window["start"] <= row["output_frame"] < window["end"]:
                _add(errors, f"{path}.output_frame", "must be inside the piece")
    seen = set()
    for i, row in enumerate(value["effects"]):
        path = f"effects[{i}]"
        effect = row["props"]
        _unique(seen, effect["type"], f"{path}.props.type", errors)
        element = _matched_anchor(row, "piece", path, words, elements, errors)
        if element:
            window = element["output"]["frames"]
            _interval(effect["startFrame"], effect["endFrame"], window["end"], f"{path}.props",
                      errors, lo=window["start"])
        if "box" in effect:
            _box_errors(effect["box"], value["canvas"]["width"], value["canvas"]["height"],
                        f"{path}.props.box", errors)
        if split and effect["type"] == "zoom":
            cap = REEL_PRESETS[value["preset"]].split_zoom_scale
            if effect["scale"] > cap:
                _add(errors, f"{path}.props.scale", f"split zoom is capped at {cap}")


def _long_timeline_errors(value, bounds, elements, words, errors):
    seen = set()
    for i, cut in enumerate(value["cuts"]):
        path = f"cuts[{i}]"
        _unique(seen, cut["id"], f"{path}.id", errors)
        proposed, actual = cut["proposed"], cut["actual"]
        _source_errors(proposed, value["inputs"]["sources"], f"{path}.proposed", errors)
        if actual is not None:
            _source_errors(actual, value["inputs"]["sources"], f"{path}.actual", errors)
            if actual["source_id"] != proposed["source_id"]:
                _add(errors, f"{path}.actual.source_id", "must match the proposed source")
        for edge in ("start", "end"):
            key = f"{edge}_shift_samples"
            shift = None if actual is None else actual["samples"][edge] - proposed["samples"][edge]
            if cut[key] != shift:
                _add(errors, f"{path}.{key}", f"expected {shift!r} (actual minus proposed)")
    seen = set()
    for i, row in enumerate(value["overlays"]):
        path = f"overlays[{i}]"
        _unique(seen, row["id"], f"{path}.id", errors)
        element = elements.get(row["element_id"])
        if not element or element["kind"] != "body":
            _add(errors, f"{path}.element_id", "expected a retained body occurrence")
        else:
            for axis in ("frames", "samples"):
                start = row["output"][axis]["start"]
                owner = element["output"][axis]
                if not owner["start"] <= start < owner["end"]:
                    _add(errors, f"{path}.output.{axis}.start", "must start inside the body occurrence")
        if "word_id" in row:
            word = words.get((row["element_id"], row["word_id"]))
            if not word or word["cue_id"] != row["cue_id"] or word["provenance"] != "matched":
                _add(errors, f"{path}.word_id", "expected a matched word from this cue and occurrence")
        _output_errors(row["output"], f"{path}.output", errors, limit=bounds)


def validate_timeline(value: object) -> list[str]:
    """Check the reel or long wire record, including clock and occurrence consistency."""
    errors = _validate(Timeline, value)
    if errors:
        return errors
    rate = value["frame_rate"]
    clock = Rational(value["sample_rate"] * rate["den"], rate["num"])
    bounds = {
        "frames": {"start": 0, "end": value["duration_frames"]},
        "samples": {"start": 0, "end": value["duration_samples"]},
    }
    if value["duration_samples"] != round(value["duration_frames"] * clock):
        _add(errors, "duration_samples", "must equal the final frame boundary on the output sample clock")
    if not value["inputs"]["sources"]:
        _add(errors, "inputs.sources", "expected at least one media source")
    elements = _timeline_elements(value, bounds, clock, errors)
    _partition(value["video_units"], clock, bounds, "video_units", errors)
    seen = set()
    for i, unit in enumerate(value["video_units"]):
        path = f"video_units[{i}]"
        _unique(seen, unit["id"], f"{path}.id", errors)
        frames = unit["output"]["frames"]
        intersects = [
            element["id"] for element in value["elements"]
            if element["output"]["frames"]["start"] < frames["end"]
            and frames["start"] < element["output"]["frames"]["end"]
        ]
        if unit["element_ids"] != intersects:
            _add(errors, f"{path}.element_ids", "expected the intersecting element IDs in output order")
    words = _timeline_words(value, elements, errors)
    if value["kind"] == "reel":
        preset = REEL_PRESETS[value["preset"]]
        if value["preset_revision"] != preset.revision:
            _add(errors, "preset_revision", f"expected {preset.revision!r} for this reel preset")
        if value["canvas"] != {"width": preset.width, "height": preset.height}:
            _add(errors, "canvas", "dimensions do not match the reel preset")
        if Rational(rate["num"], rate["den"]) != preset.fps:
            _add(errors, "frame_rate", "rate does not match the reel preset")
        _reel_timeline_errors(value, elements, words, errors)
    else:
        _long_timeline_errors(value, bounds, elements, words, errors)
    return errors


def validate_qa_record(value: object) -> list[str]:
    """Check QA status and evidence shape. Check names come from the versioned QA policy."""
    errors = _validate(QARecord, value)
    if errors:
        return errors
    if not value["checks"]:
        _add(errors, "checks", "expected at least one check")
    for check_id, check in value["checks"].items():
        path = f"checks.{check_id}"
        if check["status"] == "not_run" and "reason" not in check:
            _add(errors, f"{path}.reason", "required when status is not_run")
        if check["status"] != "not_run" and not check["evidence"]:
            _add(errors, f"{path}.evidence", "expected evidence for a measured check")
    return errors
