"""Red tests for the frame-pacing bugfix task.

All times are integer milliseconds.  A frame is ``{"pts": int, "dur": int}``.
No test sleeps or uses randomness.
"""

import math

import pytest

from playback.pacing import (
    PacingError,
    adjust_clock,
    detect_stall,
    drop_policy,
    pace_batch,
    repeat_frame,
    schedule,
    vfr_to_cfr,
    vsync_align,
)


def _frames():
    return [
        {"pts": 0, "dur": 40},
        {"pts": 40, "dur": 40},
        {"pts": 80, "dur": 40},
    ]


def test_schedule_uses_audio_clock_when_drift_exceeds_tolerance():
    times = schedule(
        _frames(),
        clock_ms=1000,
        audio_clock_ms=1200,
        max_drift_ms=50,
    )

    assert times == [1200, 1240, 1280]


def test_adjust_clock_converges_without_jump():
    adjusted = adjust_clock(
        clock_ms=1000,
        audio_clock_ms=1400,
        max_drift_ms=50,
        gain=0.25,
    )

    assert abs(adjusted - 1100) <= 1
    assert 1000 < adjusted < 1400


def test_drop_policy_uses_accumulated_lateness_and_modes():
    actions = []
    lateness = 0
    for frame_delay in (8, 8, 8):
        lateness += frame_delay
        actions.append(drop_policy(lateness, 20, "low_latency"))

    assert actions == ["show", "show", "drop"]
    assert drop_policy(20, 20, "low_latency") == "show"
    assert drop_policy(21, 20, "smooth") == "repeat"
    assert drop_policy(61, 20, "smooth") == "drop"


def test_repeat_frame_keeps_pts_and_accumulates_dur():
    frame = {"pts": 320, "dur": 40}

    repeated = repeat_frame(frame, now_ms=1080, clock_ms=1000)

    assert repeated == {"pts": 320, "dur": 120}
    assert frame == {"pts": 320, "dur": 40}


def test_detect_stall_uses_now_instead_of_adjacent_gaps():
    history = [100, 116, 132]

    assert detect_stall(history, now_ms=5000, stall_ms=1000) is True
    assert detect_stall(history, now_ms=1131, stall_ms=1000) is False
    assert detect_stall(history, now_ms=1132, stall_ms=1000) is True


def test_vsync_align_rounds_to_nearest_refresh():
    assert vsync_align(26, 60) == 33
    assert vsync_align(13, 120) == 17


def test_vfr_to_cfr_avoids_cumulative_long_clip_drift():
    frames = []
    pts = 0
    for index in range(600):
        duration = 60 if index % 3 == 0 else 40
        frames.append({"pts": pts, "dur": duration})
        pts += duration

    output = vfr_to_cfr(frames, target_fps=24)
    period_ms = 1000.0 / 24
    source_span = frames[-1]["pts"] + frames[-1]["dur"] - frames[0]["pts"]
    output_span = output[-1]["pts"] + output[-1]["dur"]

    assert len(output) == 672
    assert abs(output_span - source_span) <= period_ms + 1
    for index, frame in enumerate(output):
        assert abs(frame["pts"] - index * period_ms) <= 1


def test_pace_batch_keeps_streams_independent():
    frames = _frames()
    streams = {
        "a": {
            "frames": frames,
            "clock_ms": 1000,
            "audio_clock_ms": 1600,
        },
        "b": {
            "frames": frames,
            "clock_ms": 2000,
            "audio_clock_ms": 2000,
        },
    }

    result = pace_batch(
        streams,
        {"max_drift_ms": 50, "gain": 0.5},
    )

    assert result["a"]["clock_ms"] == 1300
    assert result["b"]["clock_ms"] == 2000
    assert result["a"]["times"] == [1600, 1640, 1680]
    assert result["b"]["times"] == [2000, 2040, 2080]


def test_schedule_respects_drift_deadband():
    times = schedule(
        _frames(),
        clock_ms=1000,
        audio_clock_ms=1040,
        max_drift_ms=50,
    )

    assert times == [1000, 1040, 1080]


def test_pace_batch_rolls_back_on_any_clock_regression():
    frames = _frames()
    streams = {
        "a": {
            "frames": frames,
            "clock_ms": 2000,
            "audio_clock_ms": 2000,
            "last_clock_ms": 2000,
        },
        "b": {
            "frames": frames,
            "clock_ms": 2900,
            "audio_clock_ms": 3000,
            "last_clock_ms": 3000,
        },
    }

    with pytest.raises(PacingError):
        pace_batch(streams, {"max_drift_ms": 50, "gain": 0.5})


def test_zero_drift_limit_follows_audio_exactly():
    times = schedule(
        _frames(),
        clock_ms=1000,
        audio_clock_ms=1010,
        max_drift_ms=0,
    )

    assert times == [1010, 1050, 1090]


def test_schedule_empty_frames_returns_empty_list():
    assert schedule([], clock_ms=1000, audio_clock_ms=9000, max_drift_ms=50) == []


def test_vsync_align_rejects_non_positive_refresh():
    for refresh_hz in (0, -60):
        with pytest.raises(PacingError):
            vsync_align(100, refresh_hz)


def test_vfr_to_cfr_total_duration_within_one_frame():
    frames = [
        {"pts": 0, "dur": 33},
        {"pts": 33, "dur": 17},
        {"pts": 50, "dur": 950},
        {"pts": 1000, "dur": 1100},
    ]
    period_ms = 1000.0 / 24

    output = vfr_to_cfr(frames, target_fps=24)
    source_span = frames[-1]["pts"] + frames[-1]["dur"] - frames[0]["pts"]
    output_span = output[-1]["pts"] + output[-1]["dur"]

    assert len(output) == math.ceil(source_span / period_ms)
    assert abs(output_span - source_span) <= period_ms + 1


def test_adjust_clock_zero_gain_leaves_clock_unchanged():
    assert adjust_clock(1000, 2000, 50, 0) == 1000
