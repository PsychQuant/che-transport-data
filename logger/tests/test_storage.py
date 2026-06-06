import os
from datetime import datetime
import duckdb
import pytest
import storage

def E(city, plate, t):
    ts = datetime.fromisoformat(t)
    return {"city": city, "plate": plate, "route_uid": "R1", "direction": 0,
            "stop_uid": "S1", "event_type": 1, "event_ts": ts, "gps_lat": 25.0,
            "gps_lon": 121.5, "captured_at": ts, "source": "A2"}

def test_refuses_write_when_not_mounted(tmp_path):
    with pytest.raises(storage.ExternalVolumeNotMounted):
        storage.write_events([E("Taipei","P1","2026-03-10T09:24:01")],
                             str(tmp_path), "arrival_event", mounted=False)
    # MUST NOT fall back: nothing written
    assert not any(tmp_path.rglob("*.parquet"))

def test_writes_hive_partitioned_by_city_date(tmp_path):
    events = [E("Taipei","P1","2026-03-10T09:24:01"),
              E("NewTaipei","P2","2026-03-11T08:00:00")]
    n = storage.write_events(events, str(tmp_path), "arrival_event", mounted=True)
    assert n == 2
    assert (tmp_path / "arrival_event" / "city=Taipei" / "date=2026-03-10").exists()
    assert (tmp_path / "arrival_event" / "city=NewTaipei" / "date=2026-03-11").exists()
    assert list(tmp_path.rglob("*.parquet"))

def test_roundtrip_readable_by_duckdb_with_partitions(tmp_path):
    events = [E("Taipei","P1","2026-03-10T09:24:01"),
              E("Taipei","P2","2026-03-10T09:25:00")]
    storage.write_events(events, str(tmp_path), "arrival_event", mounted=True)
    con = duckdb.connect()
    root = str(tmp_path / "arrival_event")
    rows = con.execute(
        f"SELECT city, count(*) FROM read_parquet('{root}/**/*.parquet', hive_partitioning=true) GROUP BY city"
    ).fetchall()
    assert dict(rows) == {"Taipei": 2}

def test_empty_events_no_write_no_error(tmp_path):
    assert storage.write_events([], str(tmp_path), "arrival_event", mounted=True) == 0

def test_volume_is_mounted_detects_root_and_rejects_plain_dir(tmp_path):
    assert storage.volume_is_mounted("/") is True          # root is a real mount
    assert storage.volume_is_mounted(str(tmp_path)) is False  # plain dir, not a mount
