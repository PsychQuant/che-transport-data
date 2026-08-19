"""Contract for wiring notify into the two entry points (issue #5).

The load-bearing case is the mount guard. Both entry points refuse an absent
NVMe with `raise SystemExit(...)`, and **SystemExit inherits from BaseException,
not Exception** — so the intuitive `except Exception` would silently miss the
single most important thing to be told about: the disk is gone.

That failure mode would recreate this issue rather than fix it, so it is pinned
here rather than left to review.

These tests drive the real entry points end to end. No credentials are set, so
notify falls back to DryRunSink and writes to stderr — which is also the exact
degradation path the jobs will run in until the alerting group exists.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "warehouse"))

import audit_partitions  # noqa: E402
import run_warehouse_sql  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Per-test state file: the state machine must not leak across tests."""
    monkeypatch.setenv("BUS_ETA_NOTIFY_STATE", str(tmp_path / "notify-state.json"))
    monkeypatch.delenv("BUS_ETA_NOTIFY_TOKEN", raising=False)
    monkeypatch.delenv("BUS_ETA_NOTIFY_CHAT_ID", raising=False)


# ── audit ──────────────────────────────────────────────────────────────────
def test_audit_reports_the_mount_guard_systemexit(tmp_path, capsys):
    missing = str(tmp_path / "nvme-not-mounted" / "parquet")
    with pytest.raises(SystemExit):
        audit_partitions.main(["--parquet-root", missing])
    err = capsys.readouterr().err
    assert "bus-eta-audit" in err
    assert "FAIL" in err


def test_audit_reports_findings_as_new_items(tmp_path, capsys):
    """Data findings now go through report_new (issue #7), not the boolean
    report(). The alert names the actual hole instead of the constant string
    "partition audit found anomalies", which forced the reader back to the log
    file nobody reads."""
    root = tmp_path / "parquet"
    for d in ("2026-08-01", "2026-08-03"):
        p = root / "arrival_event" / "city=Taipei" / f"date={d}"
        p.mkdir(parents=True)
        (p / "a.parquet").write_text("")
    rc = audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                                "--window-days", "0",
                                "--report-dir", str(tmp_path / "audit")])
    assert rc == 1                      # exit code unchanged — launchd still sees it
    err = capsys.readouterr().err
    assert "arrival_event/Taipei/missing/2026-08-02" in err
    assert "[NEW]" in err


def test_clean_audit_does_not_alert_on_its_first_healthy_run(tmp_path, capsys):
    root = tmp_path / "parquet"
    for feed in ("arrival_event", "eta_snapshot", "vehicle_position"):
        for d in ("2026-08-01", "2026-08-02"):
            p = root / feed / "city=Taipei" / f"date={d}"
            p.mkdir(parents=True)
            (p / "x.parquet").write_text("")
    rc = audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                                "--window-days", "0",
                                "--report-dir", str(tmp_path / "audit")])
    assert rc == 0
    # Do not announce "all good" on the first run — that is noise, not signal.
    assert "bus-eta-audit" not in capsys.readouterr().err


# ── warehouse ──────────────────────────────────────────────────────────────
def test_warehouse_reports_the_mount_guard_systemexit(tmp_path, capsys):
    missing = str(tmp_path / "nvme-not-mounted" / "parquet")
    with pytest.raises(SystemExit):
        run_warehouse_sql.main(["--mode", "incremental", "--db", str(tmp_path / "x.duckdb"),
                                "--parquet-root", missing, "--load-yesterday"])
    err = capsys.readouterr().err
    assert "bus-eta-warehouse" in err
    assert "FAIL" in err


def test_warehouse_mount_guard_still_exits_nonzero(tmp_path):
    # Notification must not change the job's exit behaviour — launchd still
    # needs to see the failure.
    missing = str(tmp_path / "nvme-not-mounted" / "parquet")
    with pytest.raises(SystemExit) as exc:
        run_warehouse_sql.main(["--mode", "incremental", "--db", str(tmp_path / "x.duckdb"),
                                "--parquet-root", missing, "--load-yesterday"])
    assert exc.value.code != 0


# ══════════════════════════════════════════════════════════════════════════
# B1 — argparse's SystemExit is not a job outcome (verify round 1)
# ══════════════════════════════════════════════════════════════════════════
import notify  # noqa: E402


def _state(monkeypatch_path):
    return notify.load_state(os.environ["BUS_ETA_NOTIFY_STATE"])


def test_audit_help_is_not_recorded_as_failure(capsys):
    """`--help` exits via SystemExit(0). Recording it as FAIL poisons the shared
    state, so the next real 'the disk is gone' alert is suppressed as fail->fail.
    Reproduced by three lenses independently during verify."""
    with pytest.raises(SystemExit) as exc:
        audit_partitions.main(["--help"])
    assert exc.value.code == 0
    assert "bus-eta-audit" not in capsys.readouterr().err
    assert _state(None) == {}


def test_warehouse_help_is_not_recorded_as_failure(capsys):
    with pytest.raises(SystemExit) as exc:
        run_warehouse_sql.main(["--help"])
    assert exc.value.code == 0
    assert "bus-eta-warehouse" not in capsys.readouterr().err
    assert _state(None) == {}


def test_audit_bad_argument_is_not_recorded_as_failure(capsys):
    """argparse usage errors exit 2 — also not a job outcome."""
    with pytest.raises(SystemExit):
        audit_partitions.main(["--definitely-not-a-flag"])
    assert _state(None) == {}


def test_help_does_not_mask_a_later_real_failure(tmp_path, capsys):
    """The end-to-end regression the reviewers demonstrated: an operator runs
    --help while investigating, then that night the NVMe really is gone."""
    with pytest.raises(SystemExit):
        audit_partitions.main(["--help"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        audit_partitions.main(["--parquet-root", str(tmp_path / "gone") + "/parquet"])
    err = capsys.readouterr().err
    assert "bus-eta-audit" in err and "FAIL" in err


def test_mount_guard_failure_still_preserves_exit_code(tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_warehouse_sql.main(["--mode", "incremental", "--db", str(tmp_path / "x.duckdb"),
                                "--parquet-root", str(tmp_path / "gone"), "--load-yesterday"])
    assert exc.value.code != 0


# ══════════════════════════════════════════════════════════════════════════
# B4 / B5 end-to-end (issue #7)
# ══════════════════════════════════════════════════════════════════════════
# These drive the real entry point but substitute a DELIVERING sink at the
# `notify.from_env` seam. Without it every run degrades to DryRunSink, which by
# design (B2) never consumes state — correct for the real deployment, but it
# means the suppression half of the contract could not be exercised end to end.
# RecordingSink is a real object, not a mock: it records what it was asked to
# send and nothing more.
FEEDS_ALL = ("arrival_event", "eta_snapshot", "vehicle_position")


class RecordingSink:
    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append(message)

    @property
    def text(self):
        return "\n".join(self.sent)


@pytest.fixture
def delivering(tmp_path, monkeypatch):
    sink = RecordingSink()
    state = str(tmp_path / "delivered-state.json")
    monkeypatch.setattr(notify, "from_env", lambda: (sink, state))
    return sink


def _mk(root, feeds, city, dates):
    for feed in feeds:
        for d in dates:
            p = root / feed / f"city={city}" / f"date={d}"
            p.mkdir(parents=True, exist_ok=True)
            (p / "x.parquet").write_text("")


def _audit(root, tmp_path, window="0"):
    return audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                                  "--window-days", window,
                                  "--report-dir", str(tmp_path / "audit")])


def test_window_aging_does_not_announce_recovery(tmp_path, delivering):
    """B4. A permanent hole leaving the audit window must go quiet — never
    announce a recovery, because in this system nothing recovers (TDX keeps
    ~2h). The old code sent [RECOVERED] for a hole still sitting there.

    Dates are RELATIVE to today: `recent()` windows against the real clock, so
    absolute dates would make run 2 land in the empty-directory branch instead
    of the clean-window branch — passing without ever exercising B4.
    """
    import datetime
    today = datetime.datetime.now(audit_partitions.TPE).date()
    d = lambda n: (today - datetime.timedelta(days=n)).isoformat()

    root = tmp_path / "parquet"
    _mk(root, FEEDS_ALL, "Taipei", [d(4), d(2), d(1), d(0)])   # hole at day-3
    _audit(root, tmp_path, window="0")
    assert f"missing/{d(3)}" in delivering.text
    before = len(delivering.sent)

    # Window slides past the hole: findings empty for a reason that is NOT repair.
    _audit(root, tmp_path, window="2")
    assert len(delivering.sent) == before          # nothing new said
    assert "RECOVERED" not in delivering.text


def test_worsening_findings_alert_again(tmp_path, delivering):
    """B5. A disaster tripling in size must ring again, even though the job
    stayed in the same ok/fail state throughout."""
    root = tmp_path / "parquet"
    _mk(root, ("arrival_event",), "Taipei", ["2026-08-01", "2026-08-03"])
    _mk(root, ("eta_snapshot", "vehicle_position"), "Taipei",
        ["2026-08-01", "2026-08-02", "2026-08-03"])          # these two are whole
    _audit(root, tmp_path, window="0")
    first = delivering.text
    assert "arrival_event/Taipei/missing/2026-08-02" in first

    # The other two feeds now lose the same day.
    import shutil
    for feed in ("eta_snapshot", "vehicle_position"):
        shutil.rmtree(root / feed / "city=Taipei" / "date=2026-08-02")
    _audit(root, tmp_path, window="0")

    latest = delivering.sent[-1]
    assert "eta_snapshot/Taipei/missing/2026-08-02" in latest
    assert "vehicle_position/Taipei/missing/2026-08-02" in latest
    # The already-known hole must NOT be repeated.
    assert "arrival_event/Taipei/missing/2026-08-02" not in latest


def test_same_findings_stay_silent(tmp_path, delivering):
    root = tmp_path / "parquet"
    _mk(root, ("arrival_event",), "Taipei", ["2026-08-01", "2026-08-03"])
    _mk(root, ("eta_snapshot", "vehicle_position"), "Taipei",
        ["2026-08-01", "2026-08-02", "2026-08-03"])
    _audit(root, tmp_path, window="0")
    after_first = len(delivering.sent)
    for _ in range(2):
        _audit(root, tmp_path, window="0")
    assert len(delivering.sent) == after_first


def test_dry_run_keeps_repeating_because_nothing_was_delivered(tmp_path, capsys):
    """The deployment reality until the alerting group exists. Not consuming the
    seen-set on a non-delivering sink is deliberate (same rule as B2): if a
    dry-run counted as delivered, the holes found during this window would never
    be announced once Telegram is actually wired."""
    root = tmp_path / "parquet"
    _mk(root, FEEDS_ALL, "Taipei", ["2026-08-01", "2026-08-03"])
    for _ in range(2):
        _audit(root, tmp_path, window="0")
    assert capsys.readouterr().err.count("[NEW]") == 2


def test_mount_guard_still_uses_the_boolean_report_and_does_recover(tmp_path, delivering):
    """The plan-mode correction: the job LEVEL is genuinely binary and does have
    a real recovery. The normal path must therefore still call report(ok=True),
    or the disk coming back would never be announced."""
    root = tmp_path / "parquet"
    _mk(root, FEEDS_ALL, "Taipei", ["2026-08-01", "2026-08-02"])
    with pytest.raises(SystemExit):
        audit_partitions.main(["--parquet-root", str(tmp_path / "gone")])
    assert "FAIL" in delivering.text
    _audit(root, tmp_path, window="0")
    assert "RECOVERED" in delivering.text


# ══════════════════════════════════════════════════════════════════════════
# Per-kind dispatch end-to-end (issue #7 round 2)
# ══════════════════════════════════════════════════════════════════════════

def test_error_recurrence_rings_again(tmp_path, delivering):
    """THE round-1 regression. `error` means the feed is dark RIGHT NOW — it
    recovers and it recurs. Alert-once made every episode after the first
    permanently silent, which is strictly worse than the boolean it replaced."""
    root = tmp_path / "parquet"
    root.mkdir()
    _audit(root, tmp_path, window="0")                       # dark
    assert len(delivering.sent) >= 1
    after_first = len(delivering.sent)

    _mk(root, FEEDS_ALL, "Taipei", ["2026-08-01", "2026-08-02"])   # collection back
    _audit(root, tmp_path, window="0")
    assert "RECOVERED" in delivering.text

    for feed in FEEDS_ALL:                                   # dark AGAIN
        import shutil; shutil.rmtree(root / feed)
    _audit(root, tmp_path, window="0")
    assert len(delivering.sent) > after_first + 1
    assert delivering.sent[-1].startswith("[FAIL]")


def test_error_is_per_feed_not_aggregated(tmp_path, delivering):
    """A single aggregate boolean would re-create B5: feed A rings, then feed B
    goes dark while the aggregate is already 'fail' → silence."""
    root = tmp_path / "parquet"
    _mk(root, FEEDS_ALL, "Taipei", ["2026-08-01", "2026-08-02"])
    _audit(root, tmp_path, window="0")
    import shutil
    shutil.rmtree(root / "arrival_event")
    _audit(root, tmp_path, window="0")
    n_after_a = len(delivering.sent)
    shutil.rmtree(root / "eta_snapshot")                     # second feed goes dark
    _audit(root, tmp_path, window="0")
    assert len(delivering.sent) > n_after_a
    assert "eta_snapshot" in delivering.sent[-1]


def test_prune_wiring_is_actually_connected(tmp_path, delivering, monkeypatch):
    """C6. Verify round 2 proved the whole prune_before wiring could be deleted
    with 90 tests still green — Risk 2's mitigation had ZERO entry-point
    coverage. Pre-seed a long-expired item and require the entry point to drop
    it."""
    import notify as _n
    state = str(tmp_path / "delivered-state.json")
    monkeypatch.setattr(_n, "from_env", lambda: (delivering, state))
    _n.save_state(state, {"bus-eta-audit#seen": ["arrival_event/Taipei/missing/2020-01-01"],
                          "bus-eta-audit#window": 10, "#schema": 2})
    root = tmp_path / "parquet"
    _mk(root, FEEDS_ALL, "Taipei", ["2026-08-01", "2026-08-02"])
    audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                           "--window-days", "10", "--report-dir", str(tmp_path / "audit")])
    assert "arrival_event/Taipei/missing/2020-01-01" not in \
        _n.load_state(state)["bus-eta-audit#seen"]


def test_no_notify_touches_nothing(tmp_path, capsys, monkeypatch):
    """DP5 first line of defence: manual / diagnostic runs must not write
    notification state, consume alerts nobody received, or prune at a width the
    scheduled job does not use."""
    state = tmp_path / "should-not-exist.json"
    monkeypatch.setenv("BUS_ETA_NOTIFY_STATE", str(state))
    root = tmp_path / "parquet"
    _mk(root, ("arrival_event",), "Taipei", ["2026-08-01", "2026-08-03"])
    audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                           "--window-days", "0", "--no-notify",
                           "--report-dir", str(tmp_path / "audit")])
    assert not state.exists()
    assert "[notify" not in capsys.readouterr().err
