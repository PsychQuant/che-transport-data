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


def test_audit_reports_findings_as_failure(tmp_path, capsys):
    # An audit finding IS the failure worth telling someone about — the program
    # ran fine, the data has a hole.
    root = tmp_path / "parquet"
    (root / "arrival_event" / "city=Taipei" / "date=2026-08-01").mkdir(parents=True)
    (root / "arrival_event" / "city=Taipei" / "date=2026-08-01" / "a.parquet").write_text("")
    (root / "arrival_event" / "city=Taipei" / "date=2026-08-03").mkdir(parents=True)
    (root / "arrival_event" / "city=Taipei" / "date=2026-08-03" / "b.parquet").write_text("")
    rc = audit_partitions.main(["--parquet-root", str(root), "--cities", "Taipei",
                                "--window-days", "0",
                                "--report-dir", str(tmp_path / "audit")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "bus-eta-audit" in err
    assert "FAIL" in err


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
