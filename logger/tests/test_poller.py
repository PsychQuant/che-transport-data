from datetime import datetime, timezone, timedelta
import poller
from tdx_client import Cadence

TPE = timezone(timedelta(hours=8))

def A2(plate, stop, t):
    ts = datetime.fromisoformat(t).replace(tzinfo=TPE)
    return {"city": "Taipei", "plate": plate, "route_uid": "R1", "direction": 0,
            "stop_uid": stop, "stop_sequence": 1, "event_type": 1, "gps_time": ts,
            "gps_lat": 25.0, "gps_lon": 121.5, "captured_at": ts, "source": "A2"}

class FakeClient:
    def __init__(self, a2): self._a2 = a2; self.calls = []
    def fetch_bulk(self, city, feed):
        self.calls.append((city, feed))
        return list(self._a2) if feed == "A2" else []

def test_run_cycle_fetches_dedups_writes_and_counts(tmp_path):
    # 3 A2 reports, same key within 90s → dedup to 1 event
    reports = [A2("P1","S1","2026-06-04T10:00:01"),
               A2("P1","S1","2026-06-04T10:00:31"),
               A2("P1","S1","2026-06-04T10:00:58")]
    cfg = poller.Config(cities=["Taipei"], intervals={"A2": 30},
                        volume_path="/", data_root=str(tmp_path))
    cad = Cadence({"A2": 30})
    state = poller.new_state()
    now = datetime(2026, 6, 4, 10, 1, 0, tzinfo=TPE)
    poller.run_cycle(FakeClient(reports), cad, cfg, state, now)
    assert state["dedup_total"] == 2          # 3 raw - 1 event
    assert state["cycles_with_data"] >= 1
    assert list(tmp_path.rglob("*.parquet"))  # arrival_event written

def test_run_cycle_skips_when_not_mounted(tmp_path):
    cfg = poller.Config(cities=["Taipei"], intervals={"A2": 30},
                        volume_path=str(tmp_path / "nope"), data_root=str(tmp_path))
    cad = Cadence({"A2": 30})
    state = poller.new_state()
    now = datetime(2026, 6, 4, 10, 1, 0, tzinfo=TPE)
    # not mounted → must not write, must not crash
    poller.run_cycle(FakeClient([A2("P1","S1","2026-06-04T10:00:01")]), cad, cfg, state, now)
    assert not list(tmp_path.rglob("*.parquet"))
    assert state["mount_errors"] >= 1

def test_run_cycle_only_runs_due_feeds(tmp_path):
    cfg = poller.Config(cities=["Taipei"], intervals={"A2": 30, "N1": 120},
                        volume_path="/", data_root=str(tmp_path))
    cad = Cadence({"A2": 30, "N1": 120})
    state = poller.new_state()
    c = FakeClient([A2("P1","S1","2026-06-04T10:00:01")])
    now = datetime(2026, 6, 4, 10, 0, 0, tzinfo=TPE)
    poller.run_cycle(c, cad, cfg, state, now)          # both due at t0
    feeds_first = {f for _, f in c.calls}
    assert feeds_first == {"A2", "N1"}
    c.calls.clear()
    poller.run_cycle(c, cad, cfg, state, now + timedelta(seconds=30))  # only A2 due
    assert {f for _, f in c.calls} == {"A2"}


def test_load_creds_prefers_file(tmp_path, monkeypatch):
    f = tmp_path / "tdx.json"
    f.write_text('{"client_id": "CID", "client_secret": "SEC"}')
    monkeypatch.setenv("BUS_ETA_TDX_CREDS_FILE", str(f))
    assert poller._load_creds() == ("CID", "SEC")


def test_try_token_never_raises():
    class Boom:
        def get_token(self):
            raise RuntimeError("400 unauthorized_client")
    class Ok:
        def get_token(self):
            return "tok"
    assert poller._try_token(Boom()) is False
    assert poller._try_token(Ok()) is True
