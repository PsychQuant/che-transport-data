## Context

TDX 公車動態（A2 進離站、N1 預估到站、A1 GPS）只滾動保留每車最新一筆約 2 小時即丟棄，且無任何現成歷史來源（已查證：data.taipei 即時快照原地覆蓋、GTFS-RT 封存僅靜態時刻表、學術為業者 proprietary）。要建立經驗式到站時間分布，唯一途徑是自建常駐 logger 持續採集。本變更只做採集層（capture-feasibility），不做建模/serving。執行環境：logger 跑在 mini-che（PsychQuantMini，che830621 帳號，常開），資料落該機外接 USB4 NVMe（PROBOX 盒 + Kingston NV3 2TB）。MCP 本體為 read-only stateless Swift，與本 logger 分離。

## Goals / Non-Goals

**Goals:**

- 在 TDX free tier（50 req/min）限額內，對大臺北（Taipei + NewTaipei）全公車路線**持續穩定全記** A2 到站事件 + N1 ETA baseline。
- 以 BCNF + SCD Type-2 的 schema 落地 Parquet（city/date 分區）於 mini-che 外接 NVMe；A2 去重成單一進站事件。
- 量測 capture-feasibility：coverage %、gap 分鐘數、去重正確性；spike 量 N1/A2 記錄量 + TDX `$top` 分頁以定請求預算與輪頻。
- 於 CLAUDE.md 記錄 canonical 儲存位置與目錄結構。

**Non-Goals:**

- ETA 經驗建模（P50/P80、time-of-day 分桶）與 bus_eta_predict serving tool（Phase 2，另開變更）。
- A1 GPS 站間旅行時間採集（Phase 2）。
- 大臺北以外城市；修改既有 live tool；MCP 端新增讀取 tool。

## Decisions

- **自建 logger（而非找歷史來源）**：已查證源頭 2h 即丟、無任何現成歷史，build 是唯一路徑。誰先記誰獨有。
- **Parquet + DuckDB（而非 DuckDB 原生檔 / Supabase / HDD）**：logger 是 append-only 高頻寫 + 多讀者，Parquet 不可變新檔免 writer-lock、R 的 arrow 可直讀、按 date 分區易 retention；DuckDB 為查詢引擎。DuckDB 原生檔有單一 writer 鎖；Supabase 是 OLTP serving 層、不適合大量分析寫；HDD 隨機讀慢且容量過剩。
- **BCNF + SCD Type-2 thin-fact（而非 denormalized 寬表）**：事件只存 FK + 量測 + 時間，不在百萬列重複站名 → 省儲存 + 無 update anomaly；dimension 用 SCD-2 版本化以歷史保真（事件當下的站名/座標可還原）。另可物化 denormalized 寬 view 供臨時分析（可重建、非 canonical）。
- **訊號 A2 主 + N1 baseline（A1 延後）**：A2 RealTimeNearStop 帶 A2EventType（進/離站）是最乾淨的到站真值；N1 EstimatedTimeOfArrival 當「我的模型 vs TDX」對照 baseline；A1 GPS（站間旅行時間）Phase 2。皆走 per-city bulk 以省請求。
- **Polyglot：Python 採集 / R+DuckDB 建模 / Swift serving**：Python 為爬蟲一等公民（未來擴張下注，httpx + pyarrow）；建模用 R（母語、與 l4 R+DuckDB 框架一致）；serving 為 Swift（在 MCP，Phase 2 讀預算表）。DuckDB 為跨語言中立查詢層。
- **儲存於 mini-che 外接 NVMe（而非內建 256G / 雲端）**：mini 常開適合 24/7 logger；內建系統碟僅 256G 不可被會長大的 log 塞滿；外接 NVMe 容量十餘年、Dropbox/R2 另作異地備份。
- **A2 去重規則**：同 (plate, stop_uid, direction) 在 90 秒窗內的重複回報收斂為單一進站事件，取最早 GPSTime。
- **直打 TDX raw**：抓全欄位（A2EventType / PlateNumb / StopUID / StopSequence / GPSTime），繞過 MCP 既有有損 model。

## Implementation Contract

**Behavior（operator 可觀察）**：mini-che 上一個常駐 Python 程序，依排程輪詢 TDX 大臺北 per-city bulk——A2 約 30s、N1 約 60–120s——將原始記錄去重/正規化後 append 寫入外接 NVMe 上的 Parquet 資料集，並產出 capture 指標（coverage %、gap 分鐘、去重統計）。

**Storage location / 目錄（canonical，記入 CLAUDE.md）**：根目錄位於 mini-che 外接 NVMe 掛載點下的 `che-transport/bus-eta/`（外接碟 volume 名於實機掛載後填入 CLAUDE.md）。子目錄：fact Parquet 按 `city=<code>/date=<YYYY-MM-DD>/` 分區（arrival_event / eta_snapshot）；dimension（SCD-2）獨立目錄；serving 預算表目錄保留給 Phase 2。

**Data shape（schema，詳見 spec）**：
- Dimension（SCD-2：route / stop / vehicle / route-stop bridge 版本化，含 valid_from / valid_to / is_current + surrogate key；city / operator 為 Type-1）。
- Fact（append-only，存 natural FK + event_ts + 量測）：arrival_event key (plate, route_uid, direction, stop_uid, event_type, event_ts)；eta_snapshot key (captured_at, route_uid, direction, stop_uid)。
- Fact 對 dim 以 as-of join（natural key 相同且 event_ts 落在 [valid_from, valid_to)）取版本正確列。
- SCD-2 維護排在「靜態骨架刷新」批次（每日/週拉 TDX Route/Stop/StopOfRoute、diff 現況、屬性變更則 insert 新版本），與高頻事件輪詢分離。

**Failure modes**：TDX 429 → 單次退避重試，再 429 記錄並跳過本輪（不中斷常駐）；logger 中斷 → 產生**不可回補的 gap**，必須寫 gap marker 記錄缺漏時段（避免被當成「沒有車」）；外接碟未掛載 → **拒絕寫入、不可 fallback 到系統碟**，記錯並等待掛載。

**Acceptance criteria**：(1) spike 先跑：量出 Taipei/NewTaipei A2、N1 各單輪實際記錄數 + TDX `$top` 分頁行為，產出請求預算表與選定輪頻，證明落在 50 req/min 內（或據此決定申請較高 tier）；(2) logger 連續跑 N 天（建議 ≥7），coverage（實到記錄輪數 / 應到輪數）達門檻、gap 分鐘數有完整記錄、A2 去重結果經抽樣人工核對正確；(3) 產出的 Parquet 可被 DuckDB 查詢、schema 符合 spec 的 BCNF/SCD-2 定義。

**Scope boundaries**：In scope = Python logger（A2 + N1 採集）、BCNF/SCD-2 schema 與 Parquet 落地、去重、capture 指標、feasibility spike、CLAUDE.md 儲存位置文件。Out of scope = 建模/分位數、bus_eta_predict、A1 採集、其他城市、MCP 端 tool。

## Risks / Trade-offs

- **N1 分頁逼爆請求預算**：N1（站×路線 ETA）量級大，TDX `$top` 上限可能逼出多頁分頁 → 請求數放大。緩解：A2 高頻/N1 低頻分頻；spike 先量；必要時申請較高 TDX tier。此為本變更最大不確定，故列為 acceptance 第一步 spike。
- **per-city A2 形式是否可用**：MCP 現用 per-route A2；需驗證 TDX 有 per-city bulk 形式，否則 fallback A1 per-city（複雜度上升）。
- **gap 不可回補**：snapshot 非 queryable，logger 一中斷該時段永久缺漏 → 可靠性是一等需求（常駐、掛載守衛、gap marker）。
- **NV3 DRAM-less 持續寫掉速**：對涓流寫無感（已評估），若日後高頻多城市需重評。
- **SCD-2 複雜度**：版本化 dim + as-of join 比 Type-1 複雜，但為歷史保真的必要成本；維護限批次、不影響熱路徑。
