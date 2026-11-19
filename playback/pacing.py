"""Frame pacing primitives for a playback pipeline.

All timestamps and durations exchanged here are integer milliseconds (ms).
Callers may compare returned times to expected values with a tolerance of
1 ms unless a function says otherwise.  A frame is a mapping with at least::

    {"pts": int, "dur": int}

where ``pts`` is the source presentation timestamp and ``dur`` is the source
frame duration.  Extra keys, when present, are payload and must not affect the
timing rules.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


Frame = Mapping[str, Any]


class PacingError(ValueError):
    """Raised when pacing input is invalid or a batch cannot be committed."""


def schedule(
    frames: Sequence[Frame],
    clock_ms: int,
    audio_clock_ms: int,
    max_drift_ms: int,
) -> list[int]:
    """Return the absolute display time, in ms, for every frame.

    The video timeline is ``clock_ms + frame.pts - frames[0].pts``.  Audio is
    the master clock, but the ``max_drift_ms`` deadband lets video run on its
    own when the audio/video drift is small:

    * ``abs(audio_clock_ms - clock_ms) <= max_drift_ms``: return the video
      timeline unchanged, preserving the source PTS spacing;
    * larger drift: shift the complete video timeline by
      ``audio_clock_ms - clock_ms`` so that the first frame lands on the audio
      clock.  Inter-frame spacing is still preserved.

    An empty ``frames`` sequence returns ``[]``.  ``max_drift_ms`` must be
    non-negative; malformed frames raise :class:`PacingError`.  Results are
    exact integer milliseconds and are compared with a 1 ms tolerance.
    """

    raise NotImplementedError


def adjust_clock(
    clock_ms: int,
    audio_clock_ms: int,
    max_drift_ms: int,
    gain: float,
) -> int:
    """Move the display clock toward the audio clock without a hard jump.

    ``drift = audio_clock_ms - clock_ms``.  When ``abs(drift)`` is within the
    ``max_drift_ms`` deadband, return ``clock_ms`` unchanged.  Otherwise return
    ``clock_ms + round(drift * gain)`` (rounding half away from zero).  ``gain``
    must be in ``[0, 1]``; a smaller gain converges more gradually and the
    result never overshoots the audio clock.  ``gain == 0`` always leaves the
    clock unchanged.

    All arguments are milliseconds; the result is an integer millisecond and
    is compared with a 1 ms tolerance.  Invalid gain, negative drift limit or
    malformed clocks raise :class:`PacingError`.
    """

    raise NotImplementedError


def drop_policy(late_ms: int, threshold_ms: int, mode: str) -> str:
    """Choose ``"show"``, ``"drop"`` or ``"repeat"`` for a late frame.

    ``late_ms`` is the *accumulated* lateness of the stream at this frame, not
    merely the delay of the current frame, so a series of small delays is
    handled as one growing backlog.  ``threshold_ms`` must be non-negative.

    ``"low_latency"`` keeps the stream close to real time: show while
    ``late_ms <= threshold_ms`` and drop above the threshold.

    ``"smooth"`` favours stable presentation: show through the threshold,
    repeat the previous frame while the lateness is between one and three
    thresholds, and drop only beyond three thresholds.

    The return value is exactly one of the three lowercase strings.  Lateness
    values are integer milliseconds and boundary comparisons carry a 1 ms
    tolerance.  An unsupported mode or an invalid threshold raises
    :class:`PacingError`.
    """

    raise NotImplementedError


def repeat_frame(frame: Frame, now_ms: int, clock_ms: int) -> dict[str, Any]:
    """Return a copy of ``frame`` held until ``now_ms``.

    Repeating a frame must not advance the source timeline: ``pts`` is copied
    unchanged while ``dur`` grows by the elapsed hold time
    ``now_ms - clock_ms``.  The input mapping is never mutated and any extra
    payload keys are preserved.  ``now_ms`` must not precede ``clock_ms``.

    Times are integer milliseconds; the returned ``dur`` is exact and is
    compared with a 1 ms tolerance.  A malformed frame or a negative elapsed
    time raises :class:`PacingError`.
    """

    raise NotImplementedError


def detect_stall(
    history: Sequence[int],
    now_ms: int,
    stall_ms: int,
) -> bool:
    """Report whether frame arrivals have stalled at ``now_ms``.

    ``history`` contains frame arrival timestamps in milliseconds, in
    non-decreasing order.  Stall is measured from the most recent arrival to
    ``now_ms``: return ``True`` when
    ``now_ms - history[-1] >= stall_ms``.  It deliberately does not look at
    gaps between old frames, because a paused stream would look healthy by
    that measure.  An empty history has no baseline and returns ``False``.

    ``stall_ms`` must be non-negative.  A clock that runs behind the latest
    arrival raises :class:`PacingError`.  The comparison uses integer
    milliseconds, with a 1 ms boundary tolerance.
    """

    raise NotImplementedError


def vsync_align(t_ms: int, refresh_hz: float) -> int:
    """Align ``t_ms`` to the nearest display refresh point.

    Refresh boundaries occur every ``1000 / refresh_hz`` ms.  The returned
    integer millisecond corresponds to the boundary nearest to ``t_ms``; exact
    ties go to the later boundary.  The alignment error is at most half a
    refresh period (plus at most 1 ms from returning an integer), never almost
    a whole period.

    ``t_ms`` must be non-negative and ``refresh_hz`` must be positive.
    Invalid input, including ``refresh_hz <= 0``, raises
    :class:`PacingError`.
    """

    raise NotImplementedError


def vfr_to_cfr(
    frames: Sequence[Frame],
    target_fps: float,
) -> list[dict[str, Any]]:
    """Resample a variable-frame-rate sequence onto a constant-rate grid.

    The source span is
    ``frames[-1].pts + frames[-1].dur - frames[0].pts``.  With
    ``period_ms = 1000 / target_fps``, the output contains
    ``ceil(span_ms / period_ms)`` frames (at least one) at the rounded ideal
    timestamps ``round(i * period_ms)`` starting at zero.  Each output frame's
    ``dur`` is the distance to the next rounded timestamp, so the durations add
    up to the complete target span.  The payload is a shallow copy of the
    source frame active at that source time; its ``pts`` and ``dur`` are
    replaced by the output-grid values.

    Consequently source frames are repeated or dropped as needed and the
    total output duration differs from the source span by less than one target
    frame (plus 1 ms of integer rounding), even for long clips.  An empty
    input returns ``[]``.  ``target_fps`` must be positive and the source must
    have a positive span; otherwise :class:`PacingError` is raised.
    """

    raise NotImplementedError


def pace_batch(
    streams: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Schedule multiple streams independently and atomically.

    ``streams`` maps a stream id to a mapping with:

    * ``"frames"``: that stream's frame sequence;
    * ``"clock_ms"``: its current display clock;
    * ``"audio_clock_ms"``: its current audio clock;
    * ``"last_clock_ms"`` (optional): the clock previously committed for this
      stream.  It defaults to ``clock_ms`` when omitted.

    ``config`` supplies ``"max_drift_ms"`` and ``"gain"``.  Every stream is
    evaluated with only its own frames and clocks: a stall or correction in
    one stream must not move another stream's schedule.  If *any* stream's
    ``clock_ms`` is earlier than its ``last_clock_ms``, the whole batch is
    rejected with :class:`PacingError`; no partial result is returned.

    On success the result maps every stream id to::

        {"clock_ms": <gain-adjusted clock>, "times": <absolute frame times>}

    All values are integer milliseconds and are compared with a 1 ms
    tolerance.
    """

    raise NotImplementedError
