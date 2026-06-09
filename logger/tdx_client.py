"""TDX client + poll cadence for bus-eta-logger.

OAuth client-credentials → per-city bulk A2 (RealTimeNearStop) / N1
(EstimatedTimeOfArrival). The spike proved per-city bulk returns ALL records in
one response (no $top/$skip pagination needed), so fetch_bulk issues a single
GET per city per feed.

429 handling: one backoff retry; a second 429 returns None (skip this cycle) so
the resident loop keeps running rather than terminating. Transient transport
errors (connect failure / timeout — the FailedToOpenSocket class on a flaky
link) are handled identically: retry once, then skip rather than crash.
"""
import time
from datetime import datetime, timedelta, timezone

import httpx

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
BASE = "https://tdx.transportdata.tw/api/basic"
TPE = timezone(timedelta(hours=8))  # Asia/Taipei

_FEED_PATH = {
    "A2": "/v2/Bus/RealTimeNearStop/City/{city}",
    "A1": "/v2/Bus/RealTimeByFrequency/City/{city}",
    "N1": "/v2/Bus/EstimatedTimeOfArrival/City/{city}",
}


def _parse_gpstime(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class TDXClient:
    def __init__(self, client_id=None, client_secret=None, *, http=None,
                 backoff_sec=2.0, token_url=TOKEN_URL, base=BASE):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=70)
        self.backoff_sec = backoff_sec
        self.token_url = token_url
        self.base = base
        self._token = None

    def get_token(self):
        r = self.http.post(
            self.token_url,
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        return self._token

    def _get(self, url):
        """GET with one retry on 429 OR transient transport error; then None (skip).

        A transport error (connect failure / timeout — the FailedToOpenSocket
        class seen on a flaky 24/7 link) is treated exactly like a second 429:
        retry once, then return the skip sentinel so the resident daemon keeps
        running instead of crashing on a network blip.
        """
        for attempt in (1, 2):
            try:
                r = self.http.get(url, headers={"authorization": f"Bearer {self._token}"})
            except httpx.TransportError:
                if attempt == 1:
                    time.sleep(self.backoff_sec)
                    continue
                return None  # transient network failure twice: skip cycle, do not crash
            if r.status_code == 429:
                if attempt == 1:
                    time.sleep(self.backoff_sec)
                    continue
                return None  # second 429: skip this cycle, do not crash
            r.raise_for_status()
            return r.json()
        return None

    def fetch_bulk(self, city, feed):
        """Single GET of a city's full A2/N1 feed → list of parsed records.
        Returns None if rate-limited twice (caller skips this cycle)."""
        path = _FEED_PATH[feed].format(city=city)
        data = self._get(f"{self.base}{path}?$format=JSON")
        if data is None:
            return None
        captured_at = datetime.now(TPE)
        return [self._parse(rec, city, feed, captured_at) for rec in data]

    def _parse(self, rec, city, feed, captured_at):
        if feed == "A2":
            pos = rec.get("BusPosition") or {}
            return {
                "city": city,
                "plate": rec.get("PlateNumb"),
                "route_uid": rec.get("RouteUID"),
                "direction": rec.get("Direction"),
                "stop_uid": rec.get("StopUID"),
                "stop_sequence": rec.get("StopSequence"),  # full-fidelity capture
                "event_type": rec.get("A2EventType"),
                "gps_time": _parse_gpstime(rec.get("GPSTime")),
                "gps_lat": pos.get("PositionLat"),
                "gps_lon": pos.get("PositionLon"),
                "captured_at": captured_at,
                "source": "A2",
            }
        if feed == "A1":
            pos = rec.get("BusPosition") or {}
            return {
                "city": city,
                "plate": rec.get("PlateNumb"),
                "route_uid": rec.get("RouteUID"),
                "sub_route_uid": rec.get("SubRouteUID"),
                "direction": rec.get("Direction"),
                "gps_lat": pos.get("PositionLat"),
                "gps_lon": pos.get("PositionLon"),
                "speed": rec.get("Speed"),
                "azimuth": rec.get("Azimuth"),
                "duty_status": rec.get("DutyStatus"),
                "bus_status": rec.get("BusStatus"),
                "gps_time": _parse_gpstime(rec.get("GPSTime")),
                "captured_at": captured_at,
                "source": "A1",
            }
        # N1
        return {
            "city": city,
            "route_uid": rec.get("RouteUID"),
            "direction": rec.get("Direction"),
            "stop_uid": rec.get("StopUID"),
            "estimate_time_sec": rec.get("EstimateTime"),
            "stop_status": rec.get("StopStatus"),
            "plate": rec.get("PlateNumb"),
            "captured_at": captured_at,
            "source": "N1",
        }


class Cadence:
    """Independent per-feed poll scheduler (A2 ~30s, N1 ~60-120s)."""

    def __init__(self, intervals):
        self.intervals = dict(intervals)
        self.last = {}

    def due(self, now):
        out = []
        for feed, iv in self.intervals.items():
            last = self.last.get(feed)
            if last is None or (now - last).total_seconds() >= iv:
                out.append(feed)
        return out

    def mark(self, feed, now):
        self.last[feed] = now
