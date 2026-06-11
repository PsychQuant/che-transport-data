# CWA Weather Logger

Second persistent collector on mini-che (alongside the bus-eta logger). Records
CWA 自動氣象站 observations (rain/temp/humidity/wind) for Taipei + New Taipei,
aligned to bus data for ETA modeling.

## Setup

CWA needs a free API key (`CWA-XXXX`, register at https://opendata.cwa.gov.tw/ →
會員中心 → 取得授權碼). Stored in the macOS keychain (read by the launchd daemon).

Recipe that lets the **launchd** daemon read it with NO SecurityAgent prompt —
both keychain gates must be opened (learned the hard way):
```
# 1. allow-all ACL (gate 1) via the trust-isolated dialog:
che-keychain set --service che-weather-cwa --account api_key --secure --daemon
# 2. partition list (gate 2) — needs login pw once; SecAccess API can't set it:
security set-generic-password-partition-list -S apple-tool:,apple: \
  -s che-weather-cwa -a api_key -k "$LOGIN_PW"
```
`_load_key()` reads keychain first (`security find-generic-password`, 5s timeout
guard), then a 0600 file `~/.config/weather-logger/cwa.json`, then `$CWA_API_KEY`.

## Storage
`/Volumes/mini-2TB-SSD/che-transport/weather/parquet/obs/county=<>/date=<>/*.parquet`
Reuses the bus-eta venv (`~/bus-eta-logger/.venv`, has httpx + pyarrow).

## Scope
v1 = observations (O-A0003-001). v2 = forecasts (F-D0047, the no-leakage
predict-time covariate, stored with issue-time).
