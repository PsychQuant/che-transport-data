#!/usr/bin/env python3
"""CWA weather logger — second persistent collector (mirrors bus-eta logger).

v1: observations only (CWA O-A0003-001 自動氣象站), Taipei + New Taipei, written
as Hive-partitioned Parquet to the NVMe. Aligned to bus data for ETA modeling
(rain/temp covariate). Forecast (F-D0047, the no-leakage predict-time feature)
is v2.

Creds: 0600 file ~/.config/weather-logger/cwa.json {"api_key": "CWA-xxxx"}
(register free at https://opendata.cwa.gov.tw/). Same file-not-keychain pattern
as bus-eta (launchd-friendly). Env: WEATHER_VOLUME / WEATHER_DATA_ROOT.

Run:  python weather_logger.py --probe     # dump one response's shape (verify parse)
      python weather_logger.py              # daemon loop
"""
import json, os, sys, time, uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx

TPE = timezone(timedelta(hours=8))
DATASET = "O-A0003-001"
BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"
COUNTIES = {"臺北市", "台北市", "新北市"}
OBS_INTERVAL = 600   # CWA obs update ~10 min
TICK = 30

VOLUME = os.environ.get("WEATHER_VOLUME", "/Volumes/mini-2TB-SSD")
DATA_ROOT = os.environ.get("WEATHER_DATA_ROOT", "/Volumes/mini-2TB-SSD/che-transport/weather/parquet")


def _load_key():
    path = os.environ.get("CWA_KEY_FILE", os.path.expanduser("~/.config/weather-logger/cwa.json"))
    if os.path.exists(path):
        return json.load(open(path)).get("api_key", "")
    return os.environ.get("CWA_API_KEY", "")


def fetch_obs(key, retries=1):
    url = f"{BASE}/{DATASET}"
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, params={"Authorization": key, "format": "JSON"}, timeout=40)
            if r.status_code == 200:
                return r.json()
        except httpx.TransportError:
            pass
        if attempt < retries:
            time.sleep(2)
    return None


def _wv(we, *keys):
    if isinstance(we, dict):
        for k in keys:
            if k in we and we[k] not in ("", "-99", "-990", None):
                return we[k]
    return None


def parse_obs(j, captured_at):
    """Defensive parse (new + old O-A0003-001 schema). Verify with --probe."""
    recs = j.get("records", {}) if isinstance(j, dict) else {}
    stations = recs.get("Station") or recs.get("location") or []
    out = []
    for st in stations:
        geo = st.get("GeoInfo", {}) if isinstance(st, dict) else {}
        county = geo.get("CountyName") or st.get("CountyName")
        if county not in COUNTIES:
            continue
        lat = lon = None
        for c in (geo.get("Coordinates") or []):
            if c.get("CoordinateName") in ("WGS84", None):
                lat, lon = c.get("StationLatitude"), c.get("StationLongitude")
        we = st.get("WeatherElement", {})
        now = we.get("Now") if isinstance(we, dict) else None
        precip = (now or {}).get("Precipitation") if isinstance(now, dict) else None
        ot = st.get("ObsTime", {})
        out.append({
            "station_id": st.get("StationId") or st.get("stationId"),
            "station_name": st.get("StationName") or st.get("locationName"),
            "county": county, "lat": lat, "lon": lon,
            "obs_time": ot.get("DateTime") if isinstance(ot, dict) else None,
            "air_temp": _wv(we, "AirTemperature"),
            "precip": precip,
            "humidity": _wv(we, "RelativeHumidity"),
            "wind_speed": _wv(we, "WindSpeed"),
            "weather": _wv(we, "Weather"),
            "captured_at": captured_at,
        })
    return out


def volume_mounted():
    return os.path.ismount(VOLUME)


def write_rows(rows):
    if not rows:
        return 0
    import pyarrow as pa, pyarrow.parquet as pq
    groups = defaultdict(list)
    for r in rows:
        groups[(r["county"] or "unknown", r["captured_at"][:10])].append(r)
    n = 0
    for (county, date), rs in groups.items():
        d = os.path.join(DATA_ROOT, "obs", f"county={county}", f"date={date}")
        os.makedirs(d, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rs), os.path.join(d, f"{uuid.uuid4().hex}-0.parquet"))
        n += len(rs)
    return n


def main():
    if "--probe" in sys.argv:
        key = _load_key()
        if not key:
            sys.exit("no CWA key (see header)")
        j = fetch_obs(key)
        if not j:
            sys.exit("fetch failed")
        recs = j.get("records", {})
        st = (recs.get("Station") or recs.get("location") or [])
        print("records keys:", list(recs.keys()))
        print("station count:", len(st))
        if st:
            print("first station keys:", list(st[0].keys()))
            print("first station sample:", json.dumps(st[0], ensure_ascii=False)[:800])
        print("\nparsed (Taipei/NewTaipei):", len(parse_obs(j, datetime.now(TPE).isoformat())))
        return
    key = _load_key()
    if not key:
        sys.exit("no CWA key")
    last = 0.0
    while True:
        now = time.monotonic()
        if now - last >= OBS_INTERVAL:
            if volume_mounted():
                j = fetch_obs(key, retries=1)
                if j:
                    try:
                        n = write_rows(parse_obs(j, datetime.now(TPE).isoformat()))
                    except Exception as exc:
                        print(f"write failed (non-fatal): {exc}", file=sys.stderr); n = 0
                last = now
            else:
                print("volume not mounted; skip", file=sys.stderr)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
