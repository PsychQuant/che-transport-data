from datetime import datetime, timedelta
import metrics

T0 = datetime(2026, 6, 4, 10, 0, 0)

def test_detect_gap_on_restart_after_downtime():
    g = metrics.detect_gap(T0, T0 + timedelta(minutes=30), cycle_sec=30)
    assert g is not None
    assert g["gap_start"] == T0 and g["gap_end"] == T0 + timedelta(minutes=30)
    assert g["duration_min"] == 30.0

def test_no_gap_on_normal_cycle():
    assert metrics.detect_gap(T0, T0 + timedelta(seconds=30), cycle_sec=30) is None

def test_no_gap_on_first_run():
    assert metrics.detect_gap(None, T0, cycle_sec=30) is None

def test_gap_threshold_is_two_cycles():
    # 90s elapsed with 30s cycle (> 2x=60) → gap
    assert metrics.detect_gap(T0, T0 + timedelta(seconds=90), cycle_sec=30) is not None
    # 50s elapsed (< 60) → no gap
    assert metrics.detect_gap(T0, T0 + timedelta(seconds=50), cycle_sec=30) is None

def test_dedup_count_is_collapsed_reports():
    assert metrics.dedup_count(raw_n=100, event_n=42) == 58

def test_coverage_metrics_three_values():
    m = metrics.coverage_metrics(
        cycles_with_data=95, expected_cycles=100,
        gap_markers=[{"duration_min": 5.0}, {"duration_min": 10.0}],
        dedup_total=42,
    )
    assert m["coverage_pct"] == 95.0
    assert m["total_gap_min"] == 15.0
    assert m["dedup_total"] == 42

def test_coverage_zero_expected_no_divzero():
    m = metrics.coverage_metrics(0, 0, [], 0)
    assert m["coverage_pct"] == 0.0
