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


# ══════════════════════════════════════════════════════════════════════════
# finding_items — stable identity for alert-once semantics (issue #7)
# ══════════════════════════════════════════════════════════════════════════
# Identity is feed/city/kind/date and deliberately excludes the file COUNT:
# counts fluctuate normally every day, so including them would make the audit
# ring nightly — recreating the #3 alert-fatigue silence this is meant to avoid.

def test_missing_days_become_one_item_each():
    findings = [{"feed": "arrival_event", "city": "Taipei",
                 "missing": ["2026-08-01", "2026-08-02"], "low_volume": []}]
    assert ap.finding_items(findings) == [
        "arrival_event/Taipei/missing/2026-08-01",
        "arrival_event/Taipei/missing/2026-08-02",
    ]


def test_low_volume_days_are_a_distinct_kind():
    findings = [{"feed": "eta_snapshot", "city": "NewTaipei",
                 "missing": [], "low_volume": ["2026-08-03"]}]
    assert ap.finding_items(findings) == ["eta_snapshot/NewTaipei/low_volume/2026-08-03"]


def test_same_date_missing_and_low_volume_are_different_items():
    findings = [{"feed": "f", "city": "c", "missing": ["2026-08-01"],
                 "low_volume": ["2026-08-01"]}]
    assert len(set(ap.finding_items(findings))) == 2


# NOTE: `test_error_shaped_finding_has_no_date_component` lived here and pinned
# finding_items() emitting "<feed>/<city>/error". That behaviour is deliberately
# GONE (issue #7 round 2): the error shape describes a CURRENT condition that
# recovers and recurs, so alert-once made every episode after the first
# permanently silent. It now goes through error_conditions() + report(); the new
# contract is pinned by test_finding_items_no_longer_includes_the_error_shape.


def test_multiple_findings_are_flattened_in_stable_order():
    findings = [
        {"feed": "a", "city": "Taipei", "missing": ["2026-08-02"], "low_volume": []},
        {"feed": "b", "city": "Taipei", "missing": ["2026-08-01"], "low_volume": []},
    ]
    assert ap.finding_items(findings) == [
        "a/Taipei/missing/2026-08-02", "b/Taipei/missing/2026-08-01"]


def test_no_findings_yields_no_items():
    assert ap.finding_items([]) == []


def test_identity_is_independent_of_file_counts(tmp_path):
    """Risk 1 pinned: the same hole on two nights with different surrounding
    file counts must produce the SAME item, or the audit rings every night."""
    root = tmp_path / "parquet"
    def build(counts):
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        for d, n in counts.items():
            p = root / "arrival_event" / "city=Taipei" / f"date={d}"
            p.mkdir(parents=True)
            for i in range(n):
                (p / f"{i}.parquet").write_text("")
        return ap.finding_items(ap.scan(str(root), ["Taipei"], "2026-08-05", 0.5, 0))

    # Counts must actually REACH the low_volume path, or this pins nothing:
    # 94/137 = 0.686 > min_ratio 0.5 never triggers it (verify round 2 proved
    # the original version stayed green under a count-folding mutation).
    night1 = build({"2026-08-01": 100, "2026-08-03": 100, "2026-08-04": 100})
    night2 = build({"2026-08-01": 137, "2026-08-03": 40, "2026-08-04": 137})
    assert "arrival_event/Taipei/missing/2026-08-02" in night1
    assert "arrival_event/Taipei/low_volume/2026-08-03" in night2   # path reached
    assert set(night1) - {"arrival_event/Taipei/low_volume/2026-08-03"} == \
           set(night2) - {"arrival_event/Taipei/low_volume/2026-08-03"}


# ══════════════════════════════════════════════════════════════════════════
# Per-kind dispatch (issue #7 round 2)
# ══════════════════════════════════════════════════════════════════════════
# The three finding kinds have three different temporal semantics. Round 1
# collapsed them into one and shipped a regression: `error` (this feed has no
# partitions AT ALL) genuinely recovers and genuinely recurs, so alert-once
# made every episode after the first permanently silent.

def test_finding_items_no_longer_includes_the_error_shape():
    findings = [{"feed": "f", "city": "c", "error": "no partitions found"}]
    assert ap.finding_items(findings) == []


def test_finding_items_still_covers_the_two_date_kinds():
    findings = [{"feed": "f", "city": "c", "missing": ["2026-08-01"],
                 "low_volume": ["2026-08-02"]}]
    assert ap.finding_items(findings) == [
        "f/c/missing/2026-08-01", "f/c/low_volume/2026-08-02"]


def test_error_conditions_covers_the_full_cross_product():
    """To ANNOUNCE a recovery you must report ok=True for a combination that
    previously errored — so every (feed, city) needs a verdict each run, not
    just the ones currently failing."""
    findings = [{"feed": "arrival_event", "city": "Taipei", "error": "no partitions found"}]
    out = dict(((f, c), dark) for f, c, dark in ap.error_conditions(findings, ["Taipei"]))
    assert len(out) == len(ap.FEEDS)
    assert out[("arrival_event", "Taipei")] is True
    assert out[("eta_snapshot", "Taipei")] is False


def test_error_conditions_is_per_feed_city_not_aggregated():
    """Aggregating into one boolean would re-create B5: feed A goes dark and
    rings; feed B then goes dark and the aggregate is already 'fail', so
    silence."""
    findings = [{"feed": "arrival_event", "city": "Taipei", "error": "x"},
                {"feed": "eta_snapshot", "city": "NewTaipei", "error": "x"}]
    out = dict(((f, c), d) for f, c, d in ap.error_conditions(findings, ["Taipei", "NewTaipei"]))
    assert len(out) == len(ap.FEEDS) * 2
    assert out[("arrival_event", "Taipei")] is True
    assert out[("arrival_event", "NewTaipei")] is False
