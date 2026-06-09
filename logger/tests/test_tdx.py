from datetime import datetime
import httpx
import tdx_client as tdx

TOKEN_JSON = {"access_token": "TESTTOKEN", "expires_in": 3600}
A2_SAMPLE = [{
    "PlateNumb": "EAL-5200", "StopUID": "TPE1", "RouteUID": "TPE11841",
    "Direction": 0, "StopSequence": 12, "A2EventType": 1,
    "GPSTime": "2026-06-04T14:00:01+08:00",
    "BusPosition": {"PositionLon": 121.6, "PositionLat": 25.05},
}]
N1_SAMPLE = [{
    "StopUID": "TPE1", "RouteUID": "TPE11841", "Direction": 0,
    "EstimateTime": 240, "StopStatus": 0, "PlateNumb": "EAL-5200",
}]

A1_SAMPLE = [{
    "PlateNumb": "EAL-5200", "RouteUID": "TPE11841", "SubRouteUID": "TPE118410",
    "Direction": 0, "Speed": 23, "Azimuth": 90, "DutyStatus": 0, "BusStatus": 0,
    "GPSTime": "2026-06-04T14:00:05+08:00",
    "BusPosition": {"PositionLon": 121.55, "PositionLat": 25.06},
}]

def make_client(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    c = tdx.TDXClient(client_id="id", client_secret="sec", http=http, backoff_sec=0, **kw)
    return c

def test_oauth_returns_token():
    def h(req):
        assert req.url.path.endswith("/token")
        return httpx.Response(200, json=TOKEN_JSON)
    c = make_client(h)
    assert c.get_token() == "TESTTOKEN"

def test_fetch_a2_parses_records_with_all_fields():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, json=A2_SAMPLE)
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A2")
    assert len(recs) == 1
    r = recs[0]
    assert r["plate"] == "EAL-5200" and r["stop_uid"] == "TPE1" and r["route_uid"] == "TPE11841"
    assert r["direction"] == 0 and r["event_type"] == 1
    assert r["gps_time"] == datetime.fromisoformat("2026-06-04T14:00:01+08:00")
    assert r["gps_lat"] == 25.05 and r["gps_lon"] == 121.6
    assert r["city"] == "Taipei" and r["source"] == "A2"

def test_fetch_n1_parses_eta_records():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, json=N1_SAMPLE)
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "N1")
    assert len(recs) == 1
    r = recs[0]
    assert r["stop_uid"] == "TPE1" and r["estimate_time_sec"] == 240
    assert r["stop_status"] == 0 and r["plate"] == "EAL-5200"
    assert r["city"] == "Taipei" and r["source"] == "N1"

def test_fetch_a1_parses_position_records():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, json=A1_SAMPLE)
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A1")
    assert len(recs) == 1
    r = recs[0]
    assert r["plate"] == "EAL-5200" and r["route_uid"] == "TPE11841"
    assert r["gps_lat"] == 25.06 and r["gps_lon"] == 121.55
    assert r["speed"] == 23 and r["azimuth"] == 90
    assert r["gps_time"] == datetime.fromisoformat("2026-06-04T14:00:05+08:00")
    assert r["city"] == "Taipei" and r["source"] == "A1"

def test_429_single_retry_then_success():
    calls = {"n": 0}
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        calls["n"] += 1
        return httpx.Response(429, json={"message": "rate limit"}) if calls["n"] == 1 else httpx.Response(200, json=A2_SAMPLE)
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A2")
    assert recs is not None and len(recs) == 1
    assert calls["n"] == 2  # one retry

def test_429_twice_skips_without_raising():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(429, json={"message": "rate limit"})
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A2")
    assert recs is None  # skip sentinel; process does not crash

# --- transient network failure (the FailedToOpenSocket class) ---
# A connect/timeout error must be treated like a second 429: skip the cycle,
# keep the resident daemon alive. Network blips are MORE common than 429 on a
# 24/7 logger, so an unhandled TransportError would crash poller.main's loop.
def test_transport_error_single_retry_then_success():
    calls = {"n": 0}
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("simulated socket failure")
        return httpx.Response(200, json=A2_SAMPLE)
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A2")
    assert recs is not None and len(recs) == 1
    assert calls["n"] == 2  # one retry after the transient failure

def test_transport_error_twice_skips_without_raising():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json=TOKEN_JSON)
        raise httpx.ConnectError("simulated socket failure")
    c = make_client(h); c.get_token()
    recs = c.fetch_bulk("Taipei", "A2")
    assert recs is None  # transient network failure twice → skip, no crash

# --- cadence scheduler ---
def test_cadence_a2_fires_more_often_than_n1():
    cad = tdx.Cadence({"A2": 30, "N1": 120})
    t0 = datetime(2026, 6, 4, 14, 0, 0)
    a2 = n1 = 0
    for sec in range(0, 240, 10):  # 4 minutes, tick every 10s
        now = t0.replace(second=0) + __import__("datetime").timedelta(seconds=sec)
        due = cad.due(now)
        for f in due:
            cad.mark(f, now)
            if f == "A2": a2 += 1
            else: n1 += 1
    assert a2 > n1 and n1 >= 2  # A2 ~ every 30s, N1 ~ every 120s
