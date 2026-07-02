# che-transport-data

台灣大眾運輸**資料平台**：自建採集（TDX 公車動態、CWA 氣象觀測）→ Parquet lake → DuckDB warehouse → 分析。與 MCP 工具面 [`PsychQuant/rush`](https://github.com/PsychQuant/rush) 分離（2026-07 自該 repo 分拆，git 歷史完整保留）。

## 為什麼要自己記

TDX 公車動態僅滾動保留 ~2h、無任何現成歷史來源（已查證）。源頭即丟，誰先記誰獨有。到站真值 + ETA 基準對照是 bus ETA 預測（`docs/bus-eta-prediction.md`）的地基。

## 組成

| 目錄 | 內容 | 部署 |
|------|------|------|
| `logger/` | bus-eta 採集器（Python 常駐）：A2 到站事件(30s)／A1 車輛 GPS(10s)／N1 ETA 快照(120s)，大臺北雙城 | mini-che launchd `tw.psychquant.bus-eta-logger` |
| `logger/warehouse/` | DuckDB warehouse（BCNF thin-fact + SCD2 dims + star-like views）：bootstrap／incremental／verify SQL + runner | mini 上按需執行 |
| `weather-logger/` | CWA 自動氣象站觀測採集（全台 ~362 站，10 分鐘）——ETA 天氣 covariate | mini-che launchd `tw.psychquant.weather-logger` |
| `analysis/` | `spine.sql`（ASOF 對齊 marts）、`marey.py`／`spacetime.py`／`features_gps.py`（路線視覺化與 covariate） | 筆電或 mini |
| `docs/` | ETA 預測方法論、offline serving-table 可行性 | — |
| `openspec/` | Spectra spec（`changes/bus-eta-logger` 隨拆分搬入） | — |

## 資料儲存（canonical）

mini-che 外接 NVMe：`/Volumes/mini-2TB-SSD/che-transport/{bus-eta,weather}/`。詳見 `CLAUDE.md`（分層、掛載守衛、部署三關卡）。**資料不進 git**——CSV/SQL/程式碼進版本控制，Parquet/DuckDB 為外部產物。

## 快速開始（開發）

```bash
python3 -m venv logger/.venv
logger/.venv/bin/pip install -r logger/requirements.txt
logger/.venv/bin/python -m pytest logger/tests -q
```

TDX 憑證與 rush 共用（TDX 銅級訂閱）；mini 上 daemon 讀 `~/.config/bus-eta-logger/tdx.json`（600 本機檔，不進 git）。
