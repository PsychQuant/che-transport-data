# bus-eta-logger

常駐 Python 採集器：把 TDX 公車動態（A2 進離站事件 + N1 ETA baseline）長期記錄成
Hive 分區 Parquet，供 Phase 2 建模公車到站時間分布。源頭 ~2h 即丟、無歷史可回補，
**誰先記誰獨有**。對應 Spectra change `bus-eta-logger`、issue PsychQuant/che-transport-mcp#8。

部署在 **mini-che（PsychQuantMini，常開）**，資料寫 **外接 NVMe**。與 read-only 的
che-transport MCP 完全分離。

## 模組（全部有 pytest 覆蓋）

| 檔 | 職責 |
|----|------|
| `tdx_client.py` | OAuth + per-city bulk A2/N1 抓取（單次回全部、無分頁）+ 429 退避；`Cadence` 排程 |
| `dedup.py` | A2 同 `(plate,route,dir,stop,event_type)` 90s 窗去重成單一到站事件、取最早 GPSTime |
| `storage.py` | pyarrow 寫 Hive 分區 Parquet（`city=/date=`）；**外接碟未掛載拒寫、不 fallback 系統碟** |
| `metrics.py` | gap marker（停機重啟偵測）+ coverage% / 總 gap 分鐘 / 去重計數 |
| `db.py` + `schema.sql` | DuckDB BCNF + SCD-2 schema + fact×dim as-of join view（建模層） |
| `poller.py` | 常駐 daemon：`run_cycle`（單輪）+ `main`（迴圈）|

## 前提

1. **TDX 憑證**（keychain service `che-transport-tdx`，account `client_id`/`client_secret`）：
   ```
   CheTransportMCP --setup     # 或 make setup-tdx（與 MCP 共用同一份憑證）
   ```
2. **外接 NVMe 已掛載**。logger 未掛載時**拒絕寫入**（不寫系統碟）。

## 設定（環境變數）

掛載外接碟後填入實際 volume 名：

```bash
export BUS_ETA_VOLUME="/Volumes/<NVMe-VOLUME>"
export BUS_ETA_DATA_ROOT="/Volumes/<NVMe-VOLUME>/che-transport/bus-eta/parquet"
```

輪頻預設 A2=30s、N1=120s（spike 實證 ~5 req/min ≪ TDX 50/min；見 `SPIKE_REPORT.md`）。
涵蓋 `Taipei` + `NewTaipei`。

## 本地開發 / 測試

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q          # 全套
```

## 在 mini-che 上以 launchd 常駐

`~/Library/LaunchAgents/tw.psychquant.bus-eta-logger.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>tw.psychquant.bus-eta-logger</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/che830621/path/to/logger/.venv/bin/python</string>
    <string>/Users/che830621/path/to/logger/poller.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BUS_ETA_VOLUME</key>    <string>/Volumes/<NVMe-VOLUME></string>
    <key>BUS_ETA_DATA_ROOT</key> <string>/Volumes/<NVMe-VOLUME>/che-transport/bus-eta/parquet</string>
  </dict>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>          <!-- 崩潰自動重啟；重啟會記 gap marker -->
  <key>StandardOutPath</key>  <string>/Users/che830621/Library/Logs/bus-eta-logger.out.log</string>
  <key>StandardErrorPath</key><string>/Users/che830621/Library/Logs/bus-eta-logger.err.log</string>
</dict>
</plist>
```

```bash
launchctl load  ~/Library/LaunchAgents/tw.psychquant.bus-eta-logger.plist   # 啟動
launchctl unload ~/Library/LaunchAgents/tw.psychquant.bus-eta-logger.plist  # 停止
```

> `KeepAlive` 讓崩潰自動重啟；每次重啟若跨越停機，`metrics.detect_gap` 會在
> `<data_root>/../gaps/gaps.jsonl` 記下不可回補的缺漏區間（snapshot 非 queryable，
> 中斷＝永久空洞，所以缺漏要誠實記錄而非假裝完整）。

## 驗證已採集資料（DuckDB）

```sql
SELECT city, count(*) FROM read_parquet(
  '/Volumes/<NVMe-VOLUME>/che-transport/bus-eta/parquet/arrival_event/**/*.parquet',
  hive_partitioning=true
) GROUP BY city;
```

## 範圍

MVP = **capture-feasibility**（證明大臺北全公車能在 rate-limit + 儲存內持續全覆蓋）。
ETA 準度建模（P50/P80）為 Phase 2（R + DuckDB 讀這些 Parquet）。serving 為 Swift
`bus_eta_predict` 讀預算表，不碰 raw、`source: historical`。
