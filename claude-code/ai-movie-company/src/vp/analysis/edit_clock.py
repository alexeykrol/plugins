"""Rational source-to-output timing for ordered, half-open edit occurrences."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
import re
from typing import Literal

from ..builders.contracts import OutputSpan, Ratio

Time = int | Fraction
OutputUnit = Literal["frame", "sample"]


def _exact(value: Time, path: str) -> Fraction:
    if type(value) is not int and not isinstance(value, Fraction):
        raise ValueError(f"{path}: expected an integer or Fraction")
    return Fraction(value)


@dataclass(frozen=True)
class KeptSpan:
    """A source interval in milliseconds, with nominal source/playback speed."""

    source_id: str
    start_ms: Time
    end_ms: Time
    occurrence_id: str | None = None
    rate: Time = 1


@dataclass(frozen=True)
class Gap:
    """An output interval without a source mapping, e.g. a generated card."""

    duration_ms: Time
    occurrence_id: str | None = None


@dataclass(frozen=True)
class OutputPosition:
    occurrence_id: str
    frame_position: Fraction
    sample_position: Fraction

    @property
    def frame(self) -> int:
        return round(self.frame_position)

    @property
    def sample(self) -> int:
        return round(self.sample_position)


@dataclass(frozen=True)
class SourcePosition:
    occurrence_id: str
    source_id: str
    ms: Fraction


@dataclass(frozen=True)
class EditOccurrence:
    id: str
    span: KeptSpan | Gap
    start_frame: int
    end_frame: int
    start_sample: int
    end_sample: int

    @property
    def output(self) -> OutputSpan:
        return {
            "frames": {"start": self.start_frame, "end": self.end_frame},
            "samples": {"start": self.start_sample, "end": self.end_sample},
        }

    def _project(self, source_ms: Fraction, span: KeptSpan) -> OutputPosition:
        progress = (source_ms - span.start_ms) / (span.end_ms - span.start_ms)
        return OutputPosition(
            self.id,
            self.start_frame + progress * (self.end_frame - self.start_frame),
            self.start_sample + progress * (self.end_sample - self.start_sample),
        )

    def project_range(self, start_ms: Time, end_ms: Time) -> OutputSpan | None:
        """Intersect a source interval with this occurrence and project both edges.

        The caller applies editorial word-boundary trimming before projection.
        A projected interval can be empty on either quantized grid.
        """
        start = _exact(start_ms, "start_ms")
        end = _exact(end_ms, "end_ms")
        if not 0 <= start < end:
            raise ValueError("expected a nonnegative source range with start before end")
        if isinstance(self.span, Gap):
            return None
        start = max(start, self.span.start_ms)
        end = min(end, self.span.end_ms)
        if start >= end:
            return None
        first = self._project(start, self.span)
        last = self._project(end, self.span)
        return {
            "frames": {"start": first.frame, "end": last.frame},
            "samples": {"start": first.sample, "end": last.sample},
        }


class EditClock:
    """Quantize cumulative durations, then tie each sample seam to its frame.

    Within a kept occurrence, map the fraction of source elapsed to the same
    fraction of each realized output span. This fits both endpoints after
    rounding and defines the exact inverse on each grid. Renderers using this
    mapping should use the realized span lengths when conforming the media.
    """

    def __init__(self, spans: Sequence[KeptSpan | Gap], *, frame_rate: Ratio, sample_rate: int):
        num, den = frame_rate["num"], frame_rate["den"]
        if type(num) is not int or type(den) is not int or num <= 0 or den <= 0:
            raise ValueError("frame_rate: expected a positive integer ratio")
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate: expected a positive integer")
        self._frames_per_ms = Fraction(num, 1000 * den)
        self._samples_per_frame = Fraction(sample_rate * den, num)
        self._by_id: dict[str, EditOccurrence] = {}
        self._by_source: dict[str, list[EditOccurrence]] = {}
        elapsed = Fraction(0)
        frame = sample = 0
        for index, span in enumerate(spans):
            path = f"spans[{index}]"
            occurrence_id = span.occurrence_id
            if occurrence_id is None:
                occurrence_id = f"span-{index + 1}"
            if not isinstance(occurrence_id, str) or not re.fullmatch(r"[\w-]+", occurrence_id):
                raise ValueError(f"{path}.occurrence_id: expected an identifier")
            if occurrence_id in self._by_id:
                raise ValueError(f"{path}.occurrence_id: duplicate occurrence {occurrence_id!r}")
            elapsed += self._duration(span, path)
            end_frame = round(elapsed * self._frames_per_ms)
            end_sample = self.sample_at_frame(end_frame)
            if end_frame == frame or end_sample == sample:
                raise ValueError(f"{path}: quantizes to an empty frame or sample span")
            occurrence = EditOccurrence(occurrence_id, span, frame, end_frame, sample, end_sample)
            self._by_id[occurrence_id] = occurrence
            if isinstance(span, KeptSpan):
                self._by_source.setdefault(span.source_id, []).append(occurrence)
            frame, sample = end_frame, end_sample
        self.occurrences = tuple(self._by_id.values())
        self.duration_frames, self.duration_samples = frame, sample
        self._frame_starts = tuple(row.start_frame for row in self.occurrences)
        self._sample_starts = tuple(row.start_sample for row in self.occurrences)

    @staticmethod
    def _duration(span: KeptSpan | Gap, path: str) -> Fraction:
        if isinstance(span, Gap):
            duration = _exact(span.duration_ms, f"{path}.duration_ms")
            if duration <= 0:
                raise ValueError(f"{path}.duration_ms: expected a positive duration")
            return duration
        start = _exact(span.start_ms, f"{path}.start_ms")
        end = _exact(span.end_ms, f"{path}.end_ms")
        rate = _exact(span.rate, f"{path}.rate")
        if not 0 <= start < end:
            raise ValueError(f"{path}: expected a nonnegative source range with start before end")
        if rate <= 0:
            raise ValueError(f"{path}.rate: expected a positive playback rate")
        return (end - start) / rate

    def sample_at_frame(self, frame: int) -> int:
        """The output audio sample boundary corresponding to a frame boundary."""
        if type(frame) is not int or frame < 0:
            raise ValueError("frame: expected a nonnegative integer")
        return round(frame * self._samples_per_frame)

    def source_to_output(
        self, source_id: str, source_ms: Time, *, occurrence_id: str | None = None,
    ) -> tuple[OutputPosition, ...]:
        """Map a half-open source point to every replay, in output order.

        Use the exact positions for inversion. Rounded positions denote
        boundaries and can equal the occurrence end for a point near that end.
        """
        point = _exact(source_ms, "source_ms")
        if occurrence_id is None:
            occurrences = self._by_source.get(source_id, ())
        else:
            row = self._by_id.get(occurrence_id)
            occurrences = (row,) if row is not None else ()
        positions = []
        for row in occurrences:
            span = row.span
            if (isinstance(span, KeptSpan) and span.source_id == source_id
                    and span.start_ms <= point < span.end_ms):
                positions.append(row._project(point, span))
        return tuple(positions)

    def output_to_source(self, position: Time, *, unit: OutputUnit) -> SourcePosition | None:
        """Map an exact or quantized output position back to its source occurrence.

        At a seam the next occurrence owns the position. Gaps and positions
        outside the half-open output have no source.
        """
        position = _exact(position, "position")
        if unit == "frame":
            starts, limit = self._frame_starts, self.duration_frames
        elif unit == "sample":
            starts, limit = self._sample_starts, self.duration_samples
        else:
            raise ValueError(f"unit: expected 'frame' or 'sample', got {unit!r}")
        if not 0 <= position < limit:
            return None
        index = bisect_right(starts, position) - 1
        row = self.occurrences[index]
        if isinstance(row.span, Gap):
            return None
        end = starts[index + 1] if index + 1 < len(starts) else limit
        progress = (position - starts[index]) / (end - starts[index])
        return SourcePosition(row.id, row.span.source_id,
                              row.span.start_ms + progress * (row.span.end_ms - row.span.start_ms))
