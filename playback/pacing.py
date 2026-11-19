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
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any


Frame = Mapping[str, Any]


class PacingError(ValueError):
    """Raised when pacing input is invalid or a batch cannot be committed."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: Any, name: str) -> int:
    if not _is_int(value):
        raise PacingError(f"{name} must be an integer number of milliseconds")
    return value


def _require_non_negative(value: Any, name: str) -> int:
    value = _require_int(value, name)
    if value < 0:
        raise PacingError(f"{name} must be non-negative")
    return value


def _require_frame(frame: Any, name: str = "frame") -> Frame:
    if not isinstance(frame, Mapping):
        raise PacingError(f"{name} must be a mapping")
    if "pts" not in frame or "dur" not in frame:
        raise PacingError(f"{name} must contain 'pts' and 'dur'")
    _require_int(frame["pts"], f"{name}['pts']")
    _require_int(frame["dur"], f"{name}['dur']")
    return frame


def _require_frames(frames: Any) -> Sequence[Frame]:
    if isinstance(frames, (str, bytes, Mapping)) or not isinstance(frames, Sequence):
        raise PacingError("frames must be a sequence of frame mappings")
    for index, frame in enumerate(frames):
        _require_frame(frame, f"frames[{index}]")
    return frames


def _round_half_away(value: float | Fraction) -> int:
    """Round to the nearest integer, ties away from zero."""
    if isinstance(value, Fraction):
        numerator = value.numerator
        denominator = value.denominator
    else:
        if math.isnan(value) or math.isinf(value):
            raise PacingError("cannot round a non-finite value")
        # ``Fraction(value)`` is exact for a binary float.
        fraction = Fraction(value)
        numerator = fraction.numerator
        denominator = fraction.denominator
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder >= denominator:
        quotient += 1
    return sign * quotient


def _ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


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

    clock_ms = _require_int(clock_ms, "clock_ms")
    audio_clock_ms = _require_int(audio_clock_ms, "audio_clock_ms")
    max_drift_ms = _require_non_negative(max_drift_ms, "max_drift_ms")
    _require_frames(frames)

    if not frames:
        return []

    drift = audio_clock_ms - clock_ms
    shift = 0 if abs(drift) <= max_drift_ms else drift
    origin = frames[0]["pts"]
    return [clock_ms + shift + frame["pts"] - origin for frame in frames]


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

    clock_ms = _require_int(clock_ms, "clock_ms")
    audio_clock_ms = _require_int(audio_clock_ms, "audio_clock_ms")
    max_drift_ms = _require_non_negative(max_drift_ms, "max_drift_ms")

    if isinstance(gain, bool) or not isinstance(gain, (int, float)):
        raise PacingError("gain must be a number in [0, 1]")
    gain = float(gain)
    if math.isnan(gain) or math.isinf(gain) or not 0.0 <= gain <= 1.0:
        raise PacingError("gain must be a number in [0, 1]")

    drift = audio_clock_ms - clock_ms
    if abs(drift) <= max_drift_ms or gain == 0.0:
        return clock_ms

    correction = _round_half_away(drift * gain)
    # Never overshoot the audio clock.
    if drift > 0:
        correction = min(correction, drift)
    else:
        correction = max(correction, drift)
    return clock_ms + correction


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

    late_ms = _require_int(late_ms, "late_ms")
    threshold_ms = _require_non_negative(threshold_ms, "threshold_ms")

    if mode == "low_latency":
        return "show" if late_ms <= threshold_ms else "drop"
    if mode == "smooth":
        if late_ms <= threshold_ms:
            return "show"
        if late_ms <= 3 * threshold_ms:
            return "repeat"
        return "drop"
    raise PacingError(f"unsupported drop mode: {mode!r}")


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

    frame = _require_frame(frame)
    now_ms = _require_int(now_ms, "now_ms")
    clock_ms = _require_int(clock_ms, "clock_ms")
    elapsed = now_ms - clock_ms
    if elapsed < 0:
        raise PacingError("now_ms must not precede clock_ms")

    repeated = dict(frame)
    repeated["pts"] = frame["pts"]
    repeated["dur"] = frame["dur"] + elapsed
    return repeated


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

    now_ms = _require_int(now_ms, "now_ms")
    stall_ms = _require_non_negative(stall_ms, "stall_ms")
    if isinstance(history, (str, bytes, Mapping)) or not isinstance(
        history, Sequence
    ):
        raise PacingError("history must be a sequence of timestamps")

    previous: int | None = None
    for index, arrival in enumerate(history):
        arrival = _require_int(arrival, f"history[{index}]")
        if previous is not None and arrival < previous:
            raise PacingError("history must be in non-decreasing order")
        previous = arrival

    if not history:
        return False
    if now_ms < history[-1]:
        raise PacingError("now_ms must not precede the latest arrival")
    return now_ms - history[-1] >= stall_ms


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

    t_ms = _require_non_negative(t_ms, "t_ms")
    if isinstance(refresh_hz, bool) or not isinstance(refresh_hz, (int, float)):
        raise PacingError("refresh_hz must be a positive number")
    refresh_hz = float(refresh_hz)
    if math.isnan(refresh_hz) or math.isinf(refresh_hz) or refresh_hz <= 0.0:
        raise PacingError("refresh_hz must be a positive number")

    period = Fraction(1000, 1) / Fraction(refresh_hz)
    # Index of the nearest boundary; exact halves go to the later boundary.
    index = int(math.floor(Fraction(t_ms) / period + Fraction(1, 2)))
    return _round_half_away(index * period)


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

    if isinstance(target_fps, bool) or not isinstance(target_fps, (int, float)):
        raise PacingError("target_fps must be a positive number")
    target_fps = float(target_fps)
    if math.isnan(target_fps) or math.isinf(target_fps) or target_fps <= 0.0:
        raise PacingError("target_fps must be a positive number")
    _require_frames(frames)

    if not frames:
        return []

    origin = frames[0]["pts"]
    span = frames[-1]["pts"] + frames[-1]["dur"] - origin
    if span <= 0:
        raise PacingError("source span must be positive")

    period = Fraction(1000, 1) / Fraction(target_fps)
    count = max(1, _ceil_fraction(Fraction(span) / period))

    grid = [_round_half_away(index * period) for index in range(count + 1)]

    # Source frames in order, so the active one can be found by a bisect on
    # their (relative) start times.
    starts = [frame["pts"] - origin for frame in frames]

    output: list[dict[str, Any]] = []
    for index in range(count):
        source_time = grid[index]
        active = bisect_right(starts, source_time) - 1
        if active < 0:
            active = 0
        resampled = dict(frames[active])
        resampled["pts"] = grid[index]
        resampled["dur"] = grid[index + 1] - grid[index]
        output.append(resampled)
    return output


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

    if not isinstance(streams, Mapping):
        raise PacingError("streams must be a mapping")
    if not isinstance(config, Mapping):
        raise PacingError("config must be a mapping")
    if "max_drift_ms" not in config or "gain" not in config:
        raise PacingError("config must supply 'max_drift_ms' and 'gain'")

    max_drift_ms = _require_non_negative(config["max_drift_ms"], "max_drift_ms")
    gain = config["gain"]

    # Validate everything up front: a regression in any stream rejects the
    # entire batch so no stream is partially committed.
    prepared: list[tuple[str, Sequence[Frame], int, int]] = []
    for stream_id, stream in streams.items():
        if not isinstance(stream, Mapping):
            raise PacingError(f"stream {stream_id!r} must be a mapping")
        for key in ("frames", "clock_ms", "audio_clock_ms"):
            if key not in stream:
                raise PacingError(f"stream {stream_id!r} is missing {key!r}")
        frames = _require_frames(stream["frames"])
        clock_ms = _require_int(stream["clock_ms"], "clock_ms")
        audio_clock_ms = _require_int(stream["audio_clock_ms"], "audio_clock_ms")
        last_clock_ms = _require_int(
            stream.get("last_clock_ms", clock_ms), "last_clock_ms"
        )
        if clock_ms < last_clock_ms:
            raise PacingError(
                f"stream {stream_id!r} clock regressed from "
                f"{last_clock_ms} to {clock_ms}"
            )
        prepared.append((stream_id, frames, clock_ms, audio_clock_ms))

    result: dict[str, Any] = {}
    for stream_id, frames, clock_ms, audio_clock_ms in prepared:
        adjusted = adjust_clock(clock_ms, audio_clock_ms, max_drift_ms, gain)
        times = schedule(frames, adjusted, audio_clock_ms, max_drift_ms)
        result[stream_id] = {"clock_ms": adjusted, "times": times}
    return result
