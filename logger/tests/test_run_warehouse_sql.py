"""Unit tests for run_warehouse_sql CLI plumbing (no DuckDB / no filesystem writes).

Covers the two responsibilities lifted out of the retired daily_incremental.sh
wrapper: the mount guard and the "yesterday" partition resolution. The wrapper
was removed because launchd attributes TCC to the job's Program — with /bin/sh
in front, the responsible process became /bin/sh (no Removable Volumes grant)
and every scheduled run died with EPERM for 41 days.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "warehouse"))
from run_warehouse_sql import (  # noqa: E402
    TPE, ensure_parquet_root, resolve_load_date, yesterday_taipei,
)


# ── mount guard ────────────────────────────────────────────────────────────
def test_mount_guard_accepts_existing_root(tmp_path):
    root = tmp_path / "parquet"
    root.mkdir()
    ensure_parquet_root(str(root))  # must not raise


def test_mount_guard_refuses_missing_root(tmp_path):
    missing = tmp_path / "nvme-not-mounted" / "parquet"
    with pytest.raises(SystemExit) as exc:
        ensure_parquet_root(str(missing))
    assert "not mounted" in str(exc.value)
    assert str(missing) in str(exc.value)


def test_mount_guard_refuses_file_masquerading_as_root(tmp_path):
    # A stray file where the parquet dir should be must not pass as "mounted".
    decoy = tmp_path / "parquet"
    decoy.write_text("")
    with pytest.raises(SystemExit):
        ensure_parquet_root(str(decoy))


# ── yesterday resolution (Asia/Taipei, never naive local time) ─────────────
def test_yesterday_is_one_calendar_day_back_in_taipei():
    now = datetime(2026, 8, 12, 3, 30, tzinfo=TPE)
    assert yesterday_taipei(now) == "2026-08-11"


def test_yesterday_uses_taipei_calendar_not_utc():
    # 2026-08-12T03:30+08:00 is still 2026-08-11T19:30Z. Resolving in UTC would
    # yield 2026-08-10 and silently load the wrong partition every night.
    now_utc = datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc)
    assert yesterday_taipei(now_utc) == "2026-08-11"


def test_yesterday_crosses_month_boundary():
    assert yesterday_taipei(datetime(2026, 8, 1, 3, 30, tzinfo=TPE)) == "2026-07-31"


# ── load-date resolution precedence ────────────────────────────────────────
def test_explicit_load_date_wins_over_yesterday_flag():
    now = datetime(2026, 8, 12, 3, 30, tzinfo=TPE)
    assert resolve_load_date("2026-07-04", load_yesterday=True, now=now) == "2026-07-04"


def test_yesterday_flag_fills_in_when_no_explicit_date():
    now = datetime(2026, 8, 12, 3, 30, tzinfo=TPE)
    assert resolve_load_date(None, load_yesterday=True, now=now) == "2026-08-11"


def test_no_date_and_no_flag_stays_none():
    now = datetime(2026, 8, 12, 3, 30, tzinfo=TPE)
    assert resolve_load_date(None, load_yesterday=False, now=now) is None


def test_tpe_offset_is_plus_eight():
    assert TPE.utcoffset(None) == timedelta(hours=8)
