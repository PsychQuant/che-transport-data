## Why

TDX 公車動態（A2 進離站事件 / N1 預估到站 / A1 GPS）僅滾動保留每車最新一筆約 2 小時即丟棄，且經查證無任何現成歷史來源（data.taipei 為即時快照原地覆蓋、GTFS-RT 封存只有靜態時刻表、學術為業者 proprietary 未公開）。要回答「公車何時到某站」需要實際到站時間分布，唯一取得方式是自建常駐 logger 持續採集——源頭即丟，誰先記誰獨有。本變更建立該採集層，先證「大臺北全公車能否在 TDX 限額內穩定全記」（capture-feasibility），為後續經驗式 ETA 建模奠基。對齊 North Star：補 phase = 資料採集問題（Stage 3+）。

## What Changes

- 新增**獨立常駐 Python logger**（與 read-only MCP 分離、跑在 mini-che），輪詢 TDX 大臺北（Taipei + NewTaipei）per-city bulk：**A2 RealTimeNearStop**（到站真值主力，高頻約 30s）+ **N1 EstimatedTimeOfArrival**（baseline 對照，低頻約 60–120s）。直打 TDX raw 抓全欄位（A2EventType / PlateNumb / StopUID / StopSequence / GPSTime），繞過 MCP 既有有損 model。
- 新增 **BCNF + SCD Type-2 資料庫 schema**：thin-fact（事件只存 FK + 量測 + 時間，不重複名稱）+ SCD-2 dimension（route / stop / vehicle / route-stop bridge 版本化，保歷史保真；city / operator 維持 Type-1）。
- 落地為 **Parquet（city / date 分區）+ DuckDB** 查詢；A2 以 90 秒窗對同一 (plate, stop, direction) 去重成單一進站事件、取最早 GPSTime。
- **儲存位置 = mini-che（PsychQuantMini）外接 NVMe**；於 CLAUDE.md 記錄 canonical 路徑與目錄結構。
- **capture-feasibility 驗證**：量測 coverage 百分比、gap 分鐘數、去重正確性；含一個 spike 量 N1 / A2 單輪實際記錄數 + TDX `$top` 分頁行為，據以定請求預算與輪頻。

## Non-Goals

- **ETA 經驗建模**（P50 / P80 分位數、time-of-day 分桶、平假日）與 **bus_eta_predict serving tool** — 屬 Phase 2，獨立後續變更。本變更只到「記得齊、記得穩」，不做「算得準」。
- **A1 GPS 站間旅行時間採集** — Phase 2。
- **大臺北以外城市** — 本變更 scope 僅 Taipei + NewTaipei。
- **修改既有 live tool bus_status_arrivals 行為** — 不動，保持 live 契約乾淨。
- **MCP 端讀取歷史資料** — 本變更不在 Swift MCP 新增 tool；serving 留 Phase 2。

## Capabilities

### New Capabilities

- `bus-eta-logger`: 持續採集 TDX 大臺北公車動態（A2 到站事件 + N1 ETA baseline）到 BCNF / SCD-2 的 Parquet 儲存（mini-che 外接 NVMe），並驗證在 TDX 限額內維持全覆蓋穩定採集。

### Modified Capabilities

(none)

## Impact

- Affected specs: bus-eta-logger (new)
- Affected code:
  - New: logger/ (Python 常駐採集服務), logger/schema/ (BCNF + SCD-2 schema 定義 / DuckDB DDL), logger/README.md (部署與輪頻說明)
  - Modified: CLAUDE.md (logger 儲存位置於 mini-che 外接 NVMe + 子系統說明)
  - Removed: (none)
