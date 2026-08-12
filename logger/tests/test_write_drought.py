"""Contract for write-drought escalation.

Why this exists: the gap detector had two blind spots that between them hid five
days of missing data (2026-06-13..14, 2026-07-31..08-02).

  1. The heartbeat recorded "the loop is alive", not "data was written" — so a
     mounted volume plus continuously failing cycles kept the clock ticking
     through a total drought.
  2. Gap detection ran only once, at startup. Before 2026-06-10 a crashing
     daemon restarted often enough to mask this; the (correct) fix that stopped
     the crash-loop also removed the detector's only trigger.

Writes are continuous 24/7 in this system (measured 2026-08-11: arrival_event
~112 files/hour at 03:00 as at noon, vehicle_position ~340/hour flat), so a
total drought is ALWAYS anomalous — there is no legitimate quiet window to
tolerate. Escalation policy chosen: marker first, restart only after a higher
threshold, with an uptime floor so this can never become a crash-loop.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import metrics  # noqa: E402


MARKER = 900.0     # 15 min — record a gap marker
RESTART = 1800.0   # 30 min — escalate to a self-heal restart
FLOOR = 600.0      # 10 min — minimum uptime before any restart is allowed


def classify(drought_sec, uptime_sec=3600.0, already_marked=False):
    return metrics.classify_write_drought(
        drought_sec=drought_sec,
        uptime_sec=uptime_sec,
        already_marked=already_marked,
        marker_sec=MARKER,
        restart_sec=RESTART,
        min_uptime_sec=FLOOR,
    )


# ── healthy ────────────────────────────────────────────────────────────────
def test_no_action_while_writes_are_flowing():
    assert classify(drought_sec=0.0) is None


def test_no_action_just_below_marker_threshold():
    assert classify(drought_sec=MARKER - 1) is None


# ── stage 1: marker ────────────────────────────────────────────────────────
def test_marker_at_threshold():
    assert classify(drought_sec=MARKER) == "marker"


def test_marker_between_thresholds():
    assert classify(drought_sec=1200.0) == "marker"


def test_marker_is_recorded_once_per_drought():
    # Ticking every 5s must not append a gap line every 5s.
    assert classify(drought_sec=1200.0, already_marked=True) is None


# ── stage 2: restart ───────────────────────────────────────────────────────
def test_restart_once_drought_exceeds_restart_threshold():
    assert classify(drought_sec=RESTART) == "restart"


def test_restart_still_fires_after_the_drought_was_already_marked():
    # The marker is a one-shot; escalation must not be suppressed by it.
    assert classify(drought_sec=RESTART + 1, already_marked=True) == "restart"


# ── the crash-loop floor (the 2026-06-10 disease must not return) ──────────
def test_young_process_never_restarts_even_in_deep_drought():
    # A stale last_write from before the restart makes drought huge immediately
    # on boot; restarting again here is exactly the crash-loop.
    assert classify(drought_sec=99999.0, uptime_sec=FLOOR - 1) != "restart"


def test_young_process_still_records_the_marker():
    assert classify(drought_sec=99999.0, uptime_sec=FLOOR - 1) == "marker"


def test_young_process_already_marked_does_nothing():
    assert classify(drought_sec=99999.0, uptime_sec=FLOOR - 1, already_marked=True) is None


def test_restart_allowed_exactly_at_the_uptime_floor():
    assert classify(drought_sec=RESTART, uptime_sec=FLOOR) == "restart"
