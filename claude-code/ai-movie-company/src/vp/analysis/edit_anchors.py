"""Resolve validated editorial references on one proofread source transcript."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..builders.contracts import Anchor, RangeRef, validate_anchor, validate_range_ref
from ..core.errors import VPError
from ..core.srt import Cue
from .align_words import norm

Edge = Literal["start", "end"]
WordProvenance = Literal["measured", "interpolated"]
Provenance = Literal["measured", "interpolated", "cue_edge"]
Bounds = tuple[int, int]  # Half-open source milliseconds.


class AnchorError(VPError):
    """A data error at a specific editorial or transcript field."""

    def __init__(self, path: str, message: str):
        super().__init__("data", f"{path}: {message}", path=path)
        self.path = path


@dataclass(frozen=True)
class WordTiming:
    """One aligned proofread token, in source milliseconds.

    Mark each edge measured only if its time comes from the recognized word.
    Interpolation and adjustment of a measured timestamp require separate
    evidence before that edge can certify a cut.
    """

    text: str
    start_ms: int
    end_ms: int
    start_provenance: WordProvenance
    end_provenance: WordProvenance
    source_word_id: str | None = None


@dataclass(frozen=True)
class ResolvedTime:
    ms: int
    provenance: Provenance
    cue_id: str
    word_id: str | None = None
    source_word_id: str | None = None


@dataclass(frozen=True)
class ResolvedWord:
    id: str
    text: str
    start: ResolvedTime
    end: ResolvedTime


@dataclass(frozen=True)
class ResolvedRange:
    start_ms: int
    end_ms: int
    start: ResolvedTime | None = None
    end: ResolvedTime | None = None
    fragment_id: str | None = None


def _check(errors: list[str], path: str) -> None:
    if errors:
        field, _, message = errors[0].partition(": ")
        raise AnchorError(path + field.removeprefix("$"), message)


class AnchorResolver:
    """Index one transcript and reuse it for the plan's anchors and captions.

    The caller supplies the digest of the cue artifact and the digest saved
    with the authored or accepted plan. Word rows must be bound to that same
    artifact, and contain the cue's original whitespace-delimited tokens in
    order. A cue can be absent from word_times when only its cue edges are known.
    Accepted fragments own their bounds independently of the word evidence.
    """

    def __init__(
        self, cues: Sequence[Cue], *, transcript_sha256: str, plan_transcript_sha256: str,
        word_times: Mapping[str, Sequence[WordTiming]] | None = None,
        word_times_transcript_sha256: str | None = None,
        fragments: Mapping[str, Bounds] | None = None,
    ):
        self._cues: dict[str, Cue] = {}
        self._cue_words: dict[str, list[str]] = {}
        self._word_locations: dict[str, tuple[str, int]] = {}
        self._words: dict[str, tuple[ResolvedWord, ...]] = {}
        for i, cue in enumerate(cues):
            if cue.id in self._cues:
                raise AnchorError(f"transcript.cues[{i}].id", f"duplicate cue {cue.id!r}")
            if not 0 <= cue.start_ms <= cue.end_ms:
                raise AnchorError(f"transcript.cues[{i}]", "invalid cue bounds")
            tokens = cue.text.split()
            self._cues[cue.id] = cue
            self._cue_words[cue.id] = tokens
            for index, token in enumerate(tokens):
                if norm(token):
                    self._word_locations[f"{cue.id}:w{index + 1}"] = (cue.id, index)
        self._transcript_sha256 = transcript_sha256
        self._plan_transcript_sha256 = plan_transcript_sha256
        self._word_times_sha256 = word_times_transcript_sha256
        self._word_times = {key: tuple(rows) for key, rows in (word_times or {}).items()}
        self._fragments = dict(fragments or {})

    def _check_revision(self, path: str) -> None:
        if not self._plan_transcript_sha256 or self._transcript_sha256 != self._plan_transcript_sha256:
            raise AnchorError(path, "stale anchor: plan transcript revision does not match the proofread cues")

    def words_for_cue(self, cue_id: str, *, path: str = "$") -> tuple[ResolvedWord, ...]:
        """Return aligned rows (including punctuation), or an empty tuple if unavailable."""
        self._check_revision(path)
        if cue_id not in self._cues:
            raise AnchorError(path, f"unknown cue {cue_id}")
        if cue_id in self._words:
            return self._words[cue_id]
        if self._word_times and self._word_times_sha256 != self._transcript_sha256:
            raise AnchorError(path, "stale word times: transcript revision does not match the proofread cues")
        if cue_id not in self._word_times:
            return ()
        rows = self._word_times[cue_id]
        if [row.text for row in rows] != self._cue_words[cue_id]:
            raise AnchorError(path, f"stale word times: tokens changed in cue {cue_id}")
        words = []
        for index, row in enumerate(rows, 1):
            word_id = f"{cue_id}:w{index}"
            if (type(row.start_ms) is not int or type(row.end_ms) is not int
                    or not 0 <= row.start_ms <= row.end_ms):
                raise AnchorError(path, f"invalid timing for word {word_id}")
            if (row.start_provenance not in ("measured", "interpolated")
                    or row.end_provenance not in ("measured", "interpolated")):
                raise AnchorError(path, f"unknown timing provenance for word {word_id}")
            words.append(ResolvedWord(
                word_id, row.text,
                ResolvedTime(row.start_ms, row.start_provenance, cue_id, word_id,
                             row.source_word_id if row.start_provenance == "measured" else None),
                ResolvedTime(row.end_ms, row.end_provenance, cue_id, word_id,
                             row.source_word_id if row.end_provenance == "measured" else None),
            ))
        self._words[cue_id] = tuple(words)
        return self._words[cue_id]

    def resolve_anchor(
        self, anchor: Anchor, *, edge: Edge = "start", path: str = "$",
        require_measured: bool = False, retained: Sequence[Bounds] | None = None,
    ) -> ResolvedTime:
        """Resolve a start or end edge. Restrict overlays to caller-selected retained speech.

        A start at a retained span's end lies outside that span. An end edge
        can close the span there.
        """
        if edge not in ("start", "end"):
            raise AnchorError(path, f"unknown edge {edge!r}")
        _check(validate_anchor(anchor), path)
        self._check_revision(path)
        return self._resolve(anchor, edge, path, require_measured, retained)

    def _resolve(
        self, anchor: Anchor, edge: Edge, path: str, require_measured: bool,
        retained: Sequence[Bounds] | None = None,
    ) -> ResolvedTime:
        if "word_id" in anchor:
            ref_path = f"{path}.word_id"
            word_id = anchor["word_id"]
            location = self._word_locations.get(word_id)
            if location is None:
                raise AnchorError(ref_path, f"stale anchor: word {word_id!r} is missing from the transcript")
            cue_id, index = location
            resolved = self._word_edge(cue_id, index, edge, ref_path)
        else:
            cue_id = anchor["cue_id"]
            cue = self._cues.get(cue_id)
            if cue is None:
                raise AnchorError(f"{path}.cue_id", f"stale anchor: unknown cue {cue_id}")
            if "phrase" in anchor:
                ref_path = f"{path}.phrase"
                query = [word for token in anchor["phrase"].split() if (word := norm(token))]
                tokens = [(i, word) for i, token in enumerate(self._cue_words[cue_id])
                          if (word := norm(token))]
                normalized = [word for _, word in tokens]
                matches = [i for i in range(len(tokens) - len(query) + 1)
                           if normalized[i:i + len(query)] == query]
                if not matches:
                    raise AnchorError(ref_path, f"stale anchor: phrase does not occur in cue {cue_id}")
                if "occurrence" not in anchor and len(matches) > 1:
                    raise AnchorError(ref_path, f"phrase is not unique in cue {cue_id}; give an occurrence")
                occurrence = anchor.get("occurrence", 1)
                if occurrence > len(matches):
                    raise AnchorError(f"{path}.occurrence", f"cue {cue_id} has only {len(matches)} matching occurrence(s)")
                selected = matches[occurrence - 1]
                index = tokens[selected if edge == "start" else selected + len(query) - 1][0]
                resolved = self._word_edge(cue_id, index, edge, ref_path)
            else:
                ref_path = f"{path}.cue_id"
                ms = cue.start_ms if edge == "start" else cue.end_ms
                resolved = ResolvedTime(ms, "cue_edge", cue_id)
        if require_measured and resolved.provenance != "measured":
            raise AnchorError(ref_path, f"{edge} edge is {resolved.provenance}; a measured word edge is required")
        if retained is not None:
            inside = (any(start <= resolved.ms < end for start, end in retained) if edge == "start"
                      else any(start < resolved.ms <= end for start, end in retained))
            if not inside:
                raise AnchorError(ref_path, "anchor is outside retained speech; choose a retained anchor")
        return resolved

    def _word_edge(self, cue_id: str, index: int, edge: Edge, path: str) -> ResolvedTime:
        words = self.words_for_cue(cue_id, path=path)
        if not words:
            raise AnchorError(path, f"word timing is unavailable for cue {cue_id}")
        return words[index].start if edge == "start" else words[index].end

    def resolve_range(
        self, ref: RangeRef, *, path: str = "$", envelope: Bounds | None = None,
        require_measured: bool = False,
    ) -> ResolvedRange:
        """Resolve [start, end); callers pass the applicable accepted envelope.

        Accepted bounds carry the fragment ID. They have no resolved speech
        edges, so call with require_measured only for certified speech ranges.
        """
        _check(validate_range_ref(ref), path)
        if "fragment" in ref:
            self._check_revision(f"{path}.fragment")
            fragment_id = ref["fragment"]
            bounds = self._fragments.get(fragment_id)
            if bounds is None:
                raise AnchorError(f"{path}.fragment", f"unknown accepted fragment {fragment_id!r}")
            if require_measured:
                raise AnchorError(f"{path}.fragment", "accepted bounds do not certify measured word edges")
            span = ResolvedRange(*bounds, fragment_id=fragment_id)
        else:
            start_path, end_path = f"{path}.from", f"{path}.through"
            self._check_revision(start_path)
            start = self._resolve(ref["from"], "start", start_path, require_measured)
            end = self._resolve(ref["through"], "end", end_path, require_measured)
            span = ResolvedRange(start.ms, end.ms, start, end)
        if not 0 <= span.start_ms < span.end_ms:
            raise AnchorError(path, "range needs nonnegative bounds and an end after its start")
        if envelope is not None:
            if span.start_ms < envelope[0] or span.end_ms > envelope[1]:
                raise AnchorError(path, "range is outside the accepted envelope")
        return span
