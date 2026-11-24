"""Compare a version's written and saved CapCut timelines. All output stays local."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re

from ..core.errors import VPError
from ..core.io import canonical_json, read_json, write_json
from ..core.paths import Job
from . import capcut, capcut_project

_LABELS = {
    "cut_moved": "Cut moved",
    "segment_removed": "Segment removed",
    "segment_added": "Segment added",
    "segment_reordered": "Segment reordered",
    "segment_trimmed": "Segment trimmed",
    "caption_text_changed": "Caption text changed",
    "caption_restyled": "Caption restyled",
    "caption_removed": "Caption removed",
    "caption_added": "Caption added",
    "music_volume_changed": "Music volume changed",
    "voice_volume_changed": "Voice volume changed",
    "music_replaced": "Music replaced",
    "music_removed": "Music removed",
    "transition_added": "Transition added",
    "transition_removed": "Transition removed",
    "effect_added": "Effect added",
    "speed_changed": "Speed changed",
    "canvas_changed": "Canvas changed",
    "other": "Other edit",
}
_LAYER_LABELS = {
    "picture": "Picture", "captions": "Captions", "voice": "Voice", "music": "Music", "other": "Other",
}
_EFFECT_BUCKETS = {
    "effects", "video_effects", "audio_effects", "audio_effects_v2", "material_animations",
}
_CLIP_FIELDS = ("transform", "scale", "rotation", "alpha", "flip")
_AUDIO_ROLES = {"музыка": "music", "music": "music", "голос": "voice", "voice": "voice"}
_OTHER_PROPERTIES = {
    "track": "track", "transform": "position", "scale": "scale", "rotation": "rotation",
    "alpha": "opacity", "flip": "flip", "uniform_scale": "scale lock", "reverse": "reverse",
    "visible": "visibility", "crop": "crop", "keyframes": "keyframes", "media": "media",
    "volume": "volume",
}


def _fields(obj, names):
    return {name: obj[name] for name in names if name in obj}


def _same(a, b):
    """CapCut reprints floats and floors frame times. The latter is handled in _timing."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _media(material):
    path = material.get("path") or ""
    # Absolute and draft-placeholder paths resolve to the same project-relative file.
    if "/Resources/media/" in path:
        return ("project", path.split("/Resources/media/", 1)[1])
    return ("asset", path or material.get("resource_id") or material.get("music_id"))


def _caption(material, clip):
    content = material.get("content") or {}
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = {"text": content}
    if not isinstance(content, dict):
        content = {"text": content}
    caption = str(content.get("text", material.get("text", "")) or "")
    runs = []
    styles = content.get("styles") or []
    for run in styles:
        style = _fields(run, ("size", "bold", "italic", "underline"))
        fill = (run.get("fill") or {}).get("content") or {}
        style["fill"] = _fields(fill, ("render_type", "solid"))
        font = run.get("font") or {}
        style["font"] = font.get("id") or Path(font.get("path") or "").name
        if len(styles) > 1:
            style["range"] = run.get("range")
        runs.append(style)
    top = _fields(material, ("font_size", "font_id", "font_name", "font_resource_id",
                             "text_color", "text_alpha", "alignment", "background_style",
                             "background_color", "background_alpha", "has_shadow",
                             "letter_spacing", "line_spacing", "border_color", "border_width"))
    top["font_file"] = Path(material.get("font_path") or "").name
    sizes = (next((r["size"] for r in runs if r.get("size")), None), top.get("font_size"))
    scale = abs((clip.get("scale") or {}).get("y", 1))
    return caption, (top, runs, _fields(clip, _CLIP_FIELDS)), tuple(
        size * scale if size is not None else None for size in sizes)


def _catalogue_key(material):
    return str(material.get("effect_id") or material.get("resource_id")
               or material.get("name") or material.get("id"))


def _attachments(refs):
    transitions, effects = {}, {}
    for bucket, material in refs:
        key = _catalogue_key(material)
        if bucket == "transitions":
            transitions[key] = _fields(material, ("duration", "is_overlap"))
        elif bucket == "material_animations":
            for anim in material.get("animations", []):
                effects[("animation", anim.get("type"), _catalogue_key(anim))] = (
                    _fields(anim, ("start", "duration")))
        elif bucket in _EFFECT_BUCKETS:
            effects[(bucket, key)] = _fields(material, ("value", "adjust_params", "audio_adjust_params"))
    return transitions, effects


def _keyframes(segment, *, volume):
    return {group["property_type"]: [
        _fields(point, ("time_offset", "values")) for point in group.get("keyframe_list", [])
    ] for group in segment.get("common_keyframes", [])
        if ("volume" in group["property_type"].lower()) == volume and group.get("keyframe_list")}


def _span(segment, name):
    timerange = segment.get(name) or {}
    return timerange.get("start", 0), timerange.get("duration", 0)


def _segments(info):
    materials = {m["id"]: (bucket, m) for bucket, rows in info.get("materials", {}).items()
                 if isinstance(rows, list) for m in rows if isinstance(m, dict) and m.get("id")}
    segments = {}
    for track in info["tracks"]:
        track_name = track.get("name") or ""
        track_type = track["type"]
        if track_type == "audio":
            # The writer names its music and voice tracks explicitly. Unlabelled/new audio
            # has no reliable role, so it falls back to "other".
            layer = _AUDIO_ROLES.get(track_name.strip().casefold(), "other")
        else:
            layer = {"video": "picture", "text": "captions"}.get(track_type, "other")
        ordered = sorted(track["segments"], key=lambda s: (_span(s, "target_timerange")[0], s["id"]))
        for position, segment in enumerate(ordered, 1):
            bucket, material = materials.get(segment["material_id"], ("", {}))
            segment_layer = "other" if bucket == "stickers" else layer
            refs = [materials[r] for r in segment.get("extra_material_refs", []) if r in materials]
            if bucket in _EFFECT_BUCKETS:
                refs.append((bucket, material))
            transitions, effects = _attachments(refs)
            speed = next((m for b, m in refs if b == "speeds"), {})
            clip = segment.get("clip") or {}
            segments[segment["id"]] = {
                "track": track["id"], "position": position,
                "layer": track_name or _LAYER_LABELS[segment_layer], "class": segment_layer,
                "target": _span(segment, "target_timerange"), "source": _span(segment, "source_timerange"),
                "caption": _caption(material, clip) if segment_layer == "captions" else None,
                "media": _media(material) if bucket in ("videos", "audios") else None,
                "volume": segment.get("volume", 1.0), "gain_points": _keyframes(segment, volume=True),
                "transitions": transitions, "effects": effects,
                "standalone_effect": bucket in _EFFECT_BUCKETS,
                "speed": (segment.get("speed", 1.0), speed.get("speed", 1.0),
                          speed.get("curve_speed") or None),
                "visual": {
                    **_fields(clip, () if segment_layer == "captions" else _CLIP_FIELDS),
                    **_fields(segment, ("uniform_scale", "reverse", "visible")),
                    "crop": _fields(material, ("crop", "crop_scale", "crop_ratio")),
                    "keyframes": _keyframes(segment, volume=False),
                },
            }
    return segments


def _delta(a, b, tolerance=1):
    return b - a if abs(b - a) > tolerance else 0


def _ms(us):
    return round(us / 1000, 3)


def _timing(a, b):
    start, length = a["target"]
    end = start + length
    new_start, new_length = b["target"]
    new_end = new_start + new_length
    source_start, source_length = (_delta(x, y) for x, y in zip(a["source"], b["source"]))
    return {
        "start_delta_ms": _ms(_delta(start, new_start)),
        "end_delta_ms": _ms(_delta(end, new_end, tolerance=2)),
        "duration_delta_ms": _ms(_delta(length, new_length)),
        "source_start_delta_ms": _ms(source_start),
        "source_duration_delta_ms": _ms(source_length),
    }


def _reordered(before, after):
    # Compare ranks within each track after excluding insertions, removals and transfers.
    def ranks(segments, other):
        counts = Counter()
        result = {}
        for key, segment in segments.items():
            if key in other and segment["track"] == other[key]["track"]:
                track = segment["track"]
                counts[track] += 1
                result[key] = counts[track]
        return result
    old_ranks = ranks(before, after)
    new_ranks = ranks(after, before)
    return {key for key, rank in old_ranks.items() if rank != new_ranks[key]}


def _align_music(before, after):
    """A replaced music clip can acquire a new segment ID. Pair a unique replacement slot."""
    def slots(segments, other):
        unmatched = {}
        for key, seg in segments.items():
            if key not in other and seg["class"] == "music":
                unmatched.setdefault((seg["track"], seg["target"][0]), []).append(key)
        return unmatched

    old, new = slots(before, after), slots(after, before)
    matched = {}
    for (track, start), keys in old.items():
        candidates = [key for t in (start - 1, start, start + 1) for key in new.get((track, t), [])]
        if len(keys) == len(candidates) == 1:
            key = candidates[0]
            new_start = after[key]["target"][0]
            competing = sum(len(old.get((track, t), [])) for t in
                            (new_start - 1, new_start, new_start + 1))
            if competing == 1:
                matched[key] = keys[0]
    return {matched.get(key, key): seg for key, seg in after.items()}


def _shared_cuts(before, after, reordered):
    """Find a moved shared edge between the same neighbours, once per edit."""
    def edges(segments):
        previous = {}
        shared = {}
        for key, seg in segments.items():
            track = seg["track"]
            start, duration = seg["target"]
            if seg["class"] == "picture" and track in previous:
                left, end = previous[track]
                if abs(end - start) <= 2:
                    shared[left, key] = end
            previous[track] = key, start + duration
        return shared

    old, new = edges(before), edges(after)
    return {(left, right): _ms(_delta(end, new[left, right], tolerance=2))
            for (left, right), end in old.items() if (left, right) in new
            and left not in reordered and right not in reordered
            and before[left]["track"] == after[left]["track"]
            and _delta(end, new[left, right], tolerance=2)}


def _text_extent(old, new):
    """Length of the edited span after excluding the unchanged prefix and suffix."""
    left, right = 0, 0
    limit = min(len(old), len(new))
    while left < limit and old[left] == new[left]:
        left += 1
    while right < limit - left and old[-1 - right] == new[-1 - right]:
        right += 1
    return max(len(old), len(new)) - left - right


def _compare(written, live):
    """Return local changes, their identities, and the buckets used in the signal."""
    before = _segments(written)
    after = _align_music(before, _segments(live))
    reordered = _reordered(before, after)
    shared = _shared_cuts(before, after, reordered)
    end_moves = {left: delta for (left, _right), delta in shared.items()}
    start_moves = {right: delta for (_left, right), delta in shared.items()}
    changes = []

    def emit(kind, segment, identity, value=None, *, thresholds=(0.10, 0.30), **details):
        row = {"kind": kind, "layer": segment["layer"], "position": segment["position"], **details}
        bucket = ("medium" if value is None else "small" if value < thresholds[0]
                  else "medium" if value < thresholds[1] else "large")
        digest = hashlib.sha256(canonical_json((written["id"], kind, identity)).encode()).hexdigest()
        changes.append((row, digest, (kind, segment["class"], bucket)))

    old_canvas, new_canvas = (_fields(info.get("canvas_config") or {}, ("width", "height", "ratio",
                                                                         "background"))
                              for info in (written, live))
    if not _same(old_canvas, new_canvas):
        emit("canvas_changed", {"layer": "Canvas", "class": "other", "position": None},
             (old_canvas, new_canvas))

    for key, a in before.items():
        b = after.get(key)
        layer = a["class"]
        if b is None:
            kind = {"captions": "caption_removed", "music": "music_removed"}.get(layer, "segment_removed")
            emit(kind, a, key)
            for transition in sorted(a["transitions"]):
                emit("transition_removed", a, (key, transition))
            continue
        # Written track names carry the layers.json labels. Use the new track for a transfer.
        location = b if a["track"] != b["track"] else dict(b, layer=a["layer"], **{"class": layer})
        if key in reordered:
            emit("segment_reordered", location, (key, a["position"], b["position"]),
                 abs(b["position"] - a["position"]), thresholds=(2, 4),
                 **{"from": a["position"], "to": b["position"]})
        timing = _timing(a, b)
        source_changed = timing["source_start_delta_ms"] or timing["source_duration_delta_ms"]
        # A different source or speed can change the source span without a manual trim.
        same_media = a["media"] == b["media"]
        same_speed = _same(a["speed"], b["speed"])
        if key in end_moves:
            delta = end_moves[key]
            emit("cut_moved", location, (key, "shared", delta), abs(delta),
                 thresholds=(100, 500), delta_ms=abs(delta), end_delta_ms=delta)
        shared_edge = key in end_moves or key in start_moves
        if source_changed and same_media and same_speed and not shared_edge:
            magnitude = max(abs(v) for v in timing.values())
            emit("segment_trimmed", location, (key, timing), magnitude,
                 thresholds=(100, 500), delta_ms=magnitude, **timing)
        elif key not in reordered or timing["duration_delta_ms"]:
            edges = {"start_delta_ms": timing["start_delta_ms"] if key not in start_moves else 0,
                     "end_delta_ms": timing["end_delta_ms"] if key not in end_moves else 0}
            magnitude = max(abs(n) for n in edges.values())
            if magnitude:
                emit("cut_moved", location, (key, timing), magnitude, thresholds=(100, 500),
                     delta_ms=magnitude, **edges)
        if a["caption"] is not None and b["caption"] is not None:
            old_text, old_style, old_sizes = a["caption"]
            new_text, new_style, new_sizes = b["caption"]
            if old_text != new_text:
                emit("caption_text_changed", location, (key, old_text, new_text),
                     _text_extent(old_text, new_text), thresholds=(5, 20))
            if not _same(old_style, new_style):
                ratio = next((new / old for old, new in zip(old_sizes, new_sizes)
                              if old and new is not None and not _same(old, new)), None)
                emit("caption_restyled", location, (key, old_style, new_style),
                     abs(ratio - 1) if ratio is not None else None,
                     **({"size_ratio": round(ratio, 6)} if ratio is not None else {}))
        if layer in ("music", "voice"):
            volume_changed = not _same(a["volume"], b["volume"])
            if volume_changed or not _same(a["gain_points"], b["gain_points"]):
                emit(f"{layer}_volume_changed", location,
                     (key, a["volume"], b["volume"], a["gain_points"], b["gain_points"]),
                     abs(b["volume"] - a["volume"]) if volume_changed else None,
                     **{"from": a["volume"], "to": b["volume"]})
        if layer == "music" and not same_media:
            emit("music_replaced", location, (key, a["media"], b["media"]))
        if not same_speed:
            old_speed = next((s for s in a["speed"][:2] if not _same(s, 1)), 1.0)
            new_speed = next((s for s in b["speed"][:2] if not _same(s, 1)), 1.0)
            emit("speed_changed", location, (key, a["speed"], b["speed"]),
                 abs(new_speed - old_speed) if not _same(old_speed, new_speed) else None,
                 **{"from": old_speed, "to": new_speed})

        for field, removed, added in (("transitions", "transition_removed", "transition_added"),
                                       ("effects", "other", "effect_added")):
            for item in sorted(a[field].keys() - b[field].keys()):
                details = {"detail": "Effect removed"} if removed == "other" else {}
                emit(removed, location, (key, field, item, "removed"), **details)
            for item in sorted(b[field].keys() - a[field].keys()):
                emit(added, location, (key, field, item, "added"))
            for item in sorted(a[field].keys() & b[field].keys()):
                if not _same(a[field][item], b[field][item]):
                    subject = "Transition" if field == "transitions" else "Effect"
                    emit("other", location, (key, field, item, a[field][item], b[field][item]),
                         detail=f"{subject} settings changed")
        misc_a, misc_b = [
            dict(s["visual"], track=s["track"], media=s["media"] if layer != "music" else None,
                 volume=s["volume"] if layer not in ("music", "voice") else None)
            for s in (a, b)
        ]
        modified = [name for name in _OTHER_PROPERTIES
                    if not _same(misc_a.get(name), misc_b.get(name))]
        if modified:
            properties = ", ".join(_OTHER_PROPERTIES[name] for name in modified)
            emit("other", location,
                 (key, [(name, misc_a.get(name), misc_b.get(name)) for name in modified]),
                 detail=f"Changed {properties}")

    for key, b in after.items():
        if key in before:
            continue
        if b["standalone_effect"]:
            # A standalone effect segment is itself the effect.
            kind = "effect_added"
        else:
            kind = "caption_added" if b["class"] == "captions" else "segment_added"
        emit(kind, b, key)
        for transition in sorted(b["transitions"]):
            emit("transition_added", b, (key, transition))
        if kind != "effect_added":
            for effect in sorted(b["effects"]):
                emit("effect_added", b, (key, effect))
    return changes


def _read_object(path, label):
    try:
        value = read_json(path)
    except (OSError, ValueError) as exc:
        raise VPError("data", f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise VPError("data", f"missing or invalid {label}")
    return value


def _version_dir(job, version):
    if version is None:
        completed = [n for n in capcut.versions(job)
                     if (job / "capcut" / f"v{n}" / "export.json").is_file()]
        if not completed:
            raise VPError("data", "no exported CapCut versions for this job")
        version = f"v{max(completed)}"
    return job / "capcut" / capcut.parse_version(version)


def _project(job, linked_folder):
    root = Path(linked_folder).parent if linked_folder else capcut.drafts_root()
    project = capcut_project.find_project(job, Job(job).job_id, root)
    if project is None:
        raise VPError("data", "the job's CapCut project was not found")
    return project


@contextmanager
def _locked(vdir):
    # Serialize refresh and note so a note cannot be lost between read and atomic replace.
    try:
        fd = os.open(vdir / ".diff.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
    except OSError as exc:
        raise VPError("data", "cannot write the local CapCut diff") from exc


def _timeline(path, timeline_id, label):
    info = _read_object(path, label)
    if info.get("id") != timeline_id or not isinstance(info.get("tracks"), list):
        raise VPError("data", f"invalid {label}")
    return info


def _collect_changes(project, baselines):
    registry = _read_object(project / "Timelines" / "project.json", "CapCut timeline registry")
    timeline_ids = {t["id"] for t in registry.get("timelines", []) if not t.get("is_marked_delete")}
    root_file = project / "draft_info.json"
    changes = []
    for ordinal, (timeline_id, baseline) in enumerate(baselines.items(), 1):
        if timeline_id not in timeline_ids:
            raise VPError("data", "a timeline from this version is missing in CapCut")
        timeline_file = project / "Timelines" / timeline_id / "draft_info.json"
        live = _timeline(timeline_file, timeline_id, "saved CapCut timeline")
        rows = _compare(baseline, live)
        # Current CapCut versions edit the nested timeline. A main timeline can also have
        # edits in its root copy. Accept root-only edits, or ask for a save if both disagree.
        if timeline_id == registry.get("main_timeline_id") and root_file.is_file():
            mirror = _read_object(root_file, "CapCut main timeline copy")
            if mirror.get("id") == timeline_id and isinstance(mirror.get("tracks"), list):
                mirror_rows = _compare(baseline, mirror)
                if not rows:
                    rows = mirror_rows
                elif mirror_rows and _compare(live, mirror):
                    raise VPError("data", "the two saved copies of the main timeline disagree. "
                                          "Save and close CapCut before comparing again.")
        changes.extend((dict(row, timeline=ordinal), key, signal) for row, key, signal in rows)
    return changes


def _saved_diff(vdir, *, required=False):
    path = vdir / "diff.json"
    if required or path.exists():
        return _read_object(path, "local CapCut diff (run vp capcut diff first)")
    return {}


def _save_diff(vdir, rows):
    old = _saved_diff(vdir)
    notes = {key: row["reason"] for key, row in zip(old.get("_change_keys", []),
                                                   old.get("changes", [])) if "reason" in row}
    signal_counts = Counter(signal for _row, _key, signal in rows)
    signal = [{"kind": kind, "layer": layer, "magnitude": magnitude, "count": count}
              for (kind, layer, magnitude), count in sorted(signal_counts.items())]
    result = {"version": vdir.name, "changes": [
        dict(row, change=n, **({"reason": notes[key]} if key in notes else {}))
        for n, (row, key, _signal) in enumerate(rows, 1)
    ], "signal": signal}
    write_json(vdir / "diff.json", dict(result, _change_keys=[key for _row, key, _signal in rows]))
    return result


def _description(row):
    kind = row["kind"]
    if kind in ("music_volume_changed", "voice_volume_changed") and row["from"] != row["to"]:
        sound = "Music" if kind == "music_volume_changed" else "Voice"
        return f'{sound} turned {"up" if row["to"] > row["from"] else "down"}'
    if kind == "speed_changed" and row["from"] != row["to"]:
        return "Segment sped up" if row["to"] > row["from"] else "Segment slowed down"
    if kind == "other" and row.get("detail"):
        return row["detail"]
    return _LABELS[kind]


def main(argv: list[str]) -> int:
    note = argv[:1] == ["note"]
    ap = argparse.ArgumentParser(
        prog="vp capcut diff note" if note else "vp capcut diff",
        description="Record a reason for a numbered CapCut change" if note else
                    "Compare a written version with its saved CapCut edit",
        epilog=None if note else "To record a reason, see vp capcut diff note --help.")
    ap.add_argument("job")
    ap.add_argument("--version", help="vN (default: the most recent exported version)")
    ap.add_argument("--json", action="store_true")
    if note:
        reasons = read_json(Path(__file__).with_name("capcut_diff_reasons.json"))
        ap.add_argument("--change", type=int, required=True, help="number shown by vp capcut diff")
        ap.add_argument("--reason", choices=reasons, required=True)
    args = ap.parse_args(argv[1:] if note else argv)
    job = Path(args.job).expanduser().resolve()
    vdir = _version_dir(job, args.version)
    record = _read_object(vdir / "export.json", "CapCut version record")
    link = capcut_project.read_link(job) or {}
    linked_folder = link.get("folder") or (record.get("project") or {}).get("folder")
    if linked_folder and vdir.resolve().is_relative_to(Path(linked_folder).resolve()):
        raise VPError("data", "the job's diff folder is inside the CapCut project")
    if note:
        with _locked(vdir):
            result = _saved_diff(vdir, required=True)
            changes = result.get("changes", [])
            if not 1 <= args.change <= len(changes):
                raise VPError("data", "change number is outside the saved CapCut diff")
            changes[args.change - 1]["reason"] = args.reason
            write_json(vdir / "diff.json", result)
            result = changes[args.change - 1]
    else:
        ids = [t.get("id") for t in record.get("timelines", [])]
        if not ids or any(not isinstance(i, str) or not capcut_project.UUID_RE.fullmatch(i) for i in ids):
            raise VPError("data", "the version record has no usable CapCut timeline IDs")
        baselines = {i: _timeline(vdir / capcut_project.WRITTEN_DIR / f"{i}.json", i,
                                 f"written timeline baseline for {vdir.name}") for i in ids}
        project = _project(job, linked_folder)
        if vdir.resolve().is_relative_to(project.resolve()):
            raise VPError("data", "the job's diff folder is inside the CapCut project")
        with _locked(vdir):
            try:
                result = _save_diff(vdir, _collect_changes(project, baselines))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise VPError("data", "invalid data in the CapCut timelines or diff") from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif note:
        print(f"Recorded {args.reason} for change {args.change} ({vdir.name}).")
    elif not result["changes"]:
        print(f"No saved edits in {vdir.name}.")
    else:
        print(f"Saved edits in {vdir.name}:")
        for row in result["changes"]:
            label = row["layer"]
            if re.search(r'[/\\\r\n]|\d+:\d+', label):
                label = "Track"
            location = f'{label}, segment {row["position"]}' if row["position"] else label
            if len(ids) > 1:
                location = f'timeline {row["timeline"]}, {location}'
            print(f'{row["change"]}. {_description(row)} ({location})')
    return 0
