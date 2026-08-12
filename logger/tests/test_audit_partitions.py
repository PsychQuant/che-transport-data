"""Contract for the external partition audit.

This runs as its own launchd job, deliberately NOT inside the poller: the
2026-07-31..08-02 hole went unnoticed for twelve days precisely because the
detector and the thing being detected were the same process. An audit that
depends on the collector being healthy cannot report that the collector is not.

It answers one question per feed/city: does every calendar day in the collected
span have a plausible number of parquet files?
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import audit_partitions as ap  # noqa: E402


# ── missing days ───────────────────────────────────────────────────────────
def test_contiguous_span_has_no_missing_days():
    counts = {"2026-08-09": 100, "2026-08-10": 100, "2026-08-11": 100}
    assert ap.audit_dates(counts)["missing"] == []


def test_reports_the_real_2026_07_31_hole():
    counts = {"2026-07-30": 112, "2026-08-03": 112}
    assert ap.audit_dates(counts)["missing"] == ["2026-07-31", "2026-08-01", "2026-08-02"]


def test_span_is_bounded_by_the_data_not_by_today():
    # Audit must not invent "missing" days before collection ever started.
    counts = {"2026-08-10": 100, "2026-08-11": 100}
    assert ap.audit_dates(counts)["missing"] == []


def test_empty_input_reports_nothing_rather_than_crashing():
    assert ap.audit_dates({})["missing"] == []


# ── low-volume days (a day that exists but is mostly empty) ────────────────
def test_flags_a_day_with_collapsed_volume():
    counts = {"2026-08-09": 112, "2026-08-10": 110, "2026-08-11": 3}
    assert ap.audit_dates(counts)["low_volume"] == ["2026-08-11"]


def test_normal_variation_is_not_flagged():
    counts = {"2026-08-09": 112, "2026-08-10": 104, "2026-08-11": 113}
    assert ap.audit_dates(counts)["low_volume"] == []


def test_partial_day_in_progress_is_skipped():
    # Today's partition is legitimately short; flagging it would cry wolf daily.
    counts = {"2026-08-09": 112, "2026-08-10": 110, "2026-08-11": 40}
    out = ap.audit_dates(counts, skip_dates=("2026-08-11",))
    assert out["low_volume"] == []
    assert out["missing"] == []


def test_skipped_day_does_not_drag_the_median_down():
    # A short partial day must not lower the bar such that a real outage passes.
    counts = {"2026-08-08": 112, "2026-08-09": 110, "2026-08-10": 20, "2026-08-11": 5}
    out = ap.audit_dates(counts, skip_dates=("2026-08-11",))
    assert out["low_volume"] == ["2026-08-10"]


def test_ratio_threshold_is_configurable():
    counts = {"2026-08-09": 100, "2026-08-10": 100, "2026-08-11": 70}
    assert ap.audit_dates(counts, min_ratio=0.5)["low_volume"] == []
    assert ap.audit_dates(counts, min_ratio=0.8)["low_volume"] == ["2026-08-11"]


def test_single_day_cannot_be_judged_low():
    # No baseline to compare against; must not fire on the first day of collection.
    assert ap.audit_dates({"2026-08-11": 3})["low_volume"] == []


# ── recency window (alert fatigue is the disease we are treating) ─────────
def test_window_keeps_only_recent_days():
    # Permanent historical holes must not be re-reported every single night;
    # a daily job that always says ANOMALY is a daily job nobody reads — which
    # is exactly how the 41-day warehouse outage stayed invisible.
    counts = {"2026-06-13": 100, "2026-08-10": 100, "2026-08-11": 100}
    assert ap.recent(counts, today="2026-08-12", window_days=7) == {
        "2026-08-10": 100, "2026-08-11": 100,
    }


def test_window_boundary_is_inclusive():
    counts = {"2026-08-05": 100, "2026-08-06": 100}
    # window_days=7 from 2026-08-12 → oldest kept day is 2026-08-06
    assert ap.recent(counts, today="2026-08-12", window_days=7) == {"2026-08-06": 100}


def test_window_of_zero_means_no_limit():
    counts = {"2026-06-13": 100, "2026-08-11": 100}
    assert ap.recent(counts, today="2026-08-12", window_days=0) == counts


def test_windowed_audit_still_finds_a_hole_inside_the_window():
    counts = {"2026-06-13": 100, "2026-08-08": 100, "2026-08-11": 100}
    windowed = ap.recent(counts, today="2026-08-12", window_days=7)
    out = ap.audit_dates(windowed)
    assert out["missing"] == ["2026-08-09", "2026-08-10"]


# ── verdict ────────────────────────────────────────────────────────────────
def test_clean_audit_is_not_an_anomaly():
    assert ap.has_anomalies({"missing": [], "low_volume": []}) is False


def test_any_finding_is_an_anomaly():
    assert ap.has_anomalies({"missing": ["2026-08-01"], "low_volume": []}) is True
    assert ap.has_anomalies({"missing": [], "low_volume": ["2026-08-01"]}) is True
