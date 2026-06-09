# CWA Weather Logger

Second persistent collector on mini-che (alongside the bus-eta logger). Records
CWA 自動氣象站 observations (rain/temp/humidity/wind) for Taipei + New Taipei,
aligned to bus data for ETA modeling.

## Setup
1. Register a **free** CWA Open Data key: https://opendata.cwa.gov.tw/ → 會員中心 →
   取得授權碼 (format `CWA-XXXXXXXX-...`).
2. Put it in a 0600 file on mini-che (NOT keychain — launchd-friendly, same as bus-eta):
   ```
   mkdir -p ~/.config/weather-logger
   printf '{"api_key":"CWA-XXXX"}' > ~/.config/weather-logger/cwa.json
   chmod 600 ~/.config/weather-logger/cwa.json
   ```
3. Verify parse: `~/bus-eta-logger/.venv/bin/python ~/weather-logger/weather_logger.py --probe`
4. Deploy via launchd (see tw.psychquant.weather-logger.plist) — bootstrap in the GUI
   domain so it inherits Full Disk Access (already granted to that python).

## Storage
`/Volumes/mini-2TB-SSD/che-transport/weather/parquet/obs/county=<>/date=<>/*.parquet`
Reuses the bus-eta venv (`~/bus-eta-logger/.venv`, has httpx + pyarrow).

## Scope
v1 = observations (O-A0003-001). v2 = forecasts (F-D0047, the no-leakage
predict-time covariate, stored with issue-time).
