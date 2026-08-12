"""Capture-feasibility metrics + unrecoverable-gap recording for bus-eta-logger.

Snapshots are not queryable: a logger outage = a permanent hole that can never be
backfilled. So on restart we record a gap marker covering the missed interval,
and we report coverage so feasibility can be judged honestly.
"""


def detect_gap(last_heartbeat, now, cycle_sec, *, min_gap_sec=None):
    """Return a gap marker if the logger was down across ≥1 full cycle.

    last_heartbeat None → first run ever, no gap. Threshold defaults to 2×cycle
    so normal jitter is not mistaken for an outage.
    """
    if last_heartbeat is None:
        return None
    elapsed = (now - last_heartbeat).total_seconds()
    threshold = min_gap_sec if min_gap_sec is not None else cycle_sec * 2
    if elapsed > threshold:
        return {"gap_start": last_heartbeat, "gap_end": now,
                "duration_min": round(elapsed / 60.0, 2)}
    return None


def classify_write_drought(*, drought_sec, uptime_sec, already_marked,
                           marker_sec, restart_sec, min_uptime_sec):
    """Decide what to do about a stretch of time with no successful write.

    A drought is always anomalous here: writes are continuous around the clock
    (measured flat across all 24 hours), so there is no legitimate quiet window.

    Escalation ladder:
      - below `marker_sec`                  → None      (healthy / tolerable jitter)
      - at or past `marker_sec`             → "marker"  (record the gap, keep running)
      - at or past `restart_sec`            → "restart" (exit; launchd KeepAlive relaunches)

    Two constraints keep this honest:
      - `already_marked` suppresses repeat markers within one drought — the loop
        ticks every 5s and must not append a gap line every tick. It must NOT
        suppress escalation to "restart".
      - `min_uptime_sec` is a floor on uptime before any restart is permitted.
        A restart leaves `last_write` stale, so the drought looks huge again
        immediately on boot; without this floor the daemon would restart itself
        forever. That crash-loop is the 2026-06-10 disease and must not return.

    Returns: None | "marker" | "restart"
    """
    # TODO(che): implement the ladder. Contract is pinned in
    # tests/test_write_drought.py (11 cases) — run it to check.
    raise NotImplementedError


def dedup_count(raw_n, event_n):
    """Reports collapsed by dedup = raw reports - distinct arrival events."""
    return raw_n - event_n


def coverage_metrics(cycles_with_data, expected_cycles, gap_markers, dedup_total):
    """Three feasibility metrics: coverage %, total gap minutes, dedup count."""
    coverage_pct = (100.0 * cycles_with_data / expected_cycles) if expected_cycles else 0.0
    total_gap_min = sum(g["duration_min"] for g in gap_markers)
    return {
        "coverage_pct": round(coverage_pct, 2),
        "total_gap_min": round(total_gap_min, 2),
        "dedup_total": dedup_total,
        "cycles_with_data": cycles_with_data,
        "expected_cycles": expected_cycles,
    }
