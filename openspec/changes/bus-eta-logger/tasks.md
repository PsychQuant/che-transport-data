## 1. Feasibility Spike（命門，最先做）

- [x] 1.1 量測 Taipei 與 NewTaipei 的 A2、N1 各單輪實際記錄數與 TDX `$top` 分頁行為，產出請求預算表與選定的 A2/N1 輪頻，並證明穩態請求率 ≤ 適用 TDX 限額（spec: Rate-Limit Adherence and Feasibility Budget）（驗證：spike 報告列每城每 feed 記錄數、頁數、req/min 計算結果，且 ≤50/min 或據此標記需申請較高 tier）

## 2. Schema 定義（BCNF + SCD-2）

- [x] 2.1 [P] 定義 SCD-2 dimension schema（route / stop / vehicle / route-stop bridge：surrogate key + valid_from / valid_to / is_current；city / operator 為 Type-1）並以 DuckDB DDL 落在 logger schema 目錄（spec: BCNF and SCD Type-2 Storage Schema）（驗證：DuckDB 成功建表，且逐表確認每個 determinant 為 candidate key 滿足 BCNF）
- [x] 2.2 [P] 定義 thin-fact schema（arrival_event、eta_snapshot：natural FK + 量測 + event_ts / captured_at，無任何描述名稱欄）並以 DuckDB DDL 落地（spec: BCNF and SCD Type-2 Storage Schema）（驗證：DuckDB 建表成功，且確認 fact 表不含 stop_name / route_name 欄）
- [x] 2.3 定義 fact × SCD-2 dim 的 as-of join 查詢（natural key 相同且 event_ts 落在 [valid_from, valid_to)）（spec: BCNF and SCD Type-2 Storage Schema）（驗證：對樣本資料執行，事件對回正確版本，通過 spec 的 as-of join Example 案例）

## 3. Ingestion（Python 常駐採集）

- [x] 3.1 實作 TDX OAuth client-credentials 取 token 與 per-city bulk A2 / N1 raw 抓取（httpx），抓齊全欄位（spec: Full-Fidelity Raw Field Capture）（驗證：對 Taipei 實際取回 A2 記錄且含 A2EventType / PlateNumb / StopUID / StopSequence / GPSTime）
- [x] 3.2 實作分頁處理（依 spike 結果以 `$top` / `$skip` 取齊整城記錄）（spec: Full-Fidelity Raw Field Capture）（驗證：單城 A2 取回總筆數等於各頁加總、無漏頁） 〔spike 修正：TDX per-city bulk 單次回全部、無需 `$top`/`$skip` 分頁；改為單次 GET 完整擷取，fetch_bulk 取回筆數＝整城總數（Taipei A2=1511 已 spike 實證 default==top100k）〕
- [x] 3.3 實作排程常駐迴圈（A2 約 30s、N1 約 60–120s 獨立 cadence）（spec: Continuous Dual-Signal Capture）（驗證：連跑 10 分鐘，A2 輪數多於 N1 輪數且符合設定 cadence）
- [x] 3.4 實作 TDX 429 處理（單次退避重試；二次 429 記錄並跳過本輪、不終止程序）（spec: Rate-Limit Adherence and Feasibility Budget）（驗證：注入模擬 429，程序持續運行且有事件記錄）
- [x] 3.5 實作暫時性傳輸層失敗韌性（connect / timeout，即 `FailedToOpenSocket` 類）：`_get` 比照 429——單次退避重試後回 None 跳過本輪、不終止程序（spec: Continuous Dual-Signal Capture）（驗證：注入 `httpx.TransportError`，單次失敗→重試後成功、連續失敗→`fetch_bulk` 回 None 不拋例外；`tests/test_tdx.py` 兩個 `transport_error` 測試）〔備註：`poller.main` 的 token-refresh 崩潰窗較小、牽涉 token 過期重試時機，列後續項；現靠 launchd KeepAlive + gap marker 自癒〕

## 4. Storage（Parquet + 外接碟守衛）

- [x] 4.1 [P] 實作以 pyarrow 寫 Parquet 並按 city / date 分區到 mini-che 外接 NVMe canonical 根（spec: Partitioned Parquet on Designated External Storage）（驗證：寫入後路徑符合 city=<code>/date=<YYYY-MM-DD>/ 且 DuckDB 可讀回）
- [x] 4.2 [P] 實作外接碟掛載守衛：未掛載則拒絕寫入、不 fallback 系統碟、記錄錯誤（spec: Partitioned Parquet on Designated External Storage）（驗證：在外接碟卸載狀態執行，系統碟無新檔且有錯誤記錄）

## 5. Dedup

- [x] 5.1 實作 A2 以 90 秒窗對同一 (plate, stop_uid, direction) 去重成單一進站事件並取最早 GPSTime（spec: Arrival-Event Deduplication）（驗證：通過 spec 的「三筆報告 → 一筆事件、取最早 GPSTime」Example 案例）

## 6. Metrics 與 Gap

- [x] 6.1 實作 gap marker：偵測停機後重啟並記錄涵蓋缺漏區間的標記（spec: Unrecoverable-Gap Recording）（驗證：模擬停機再重啟，產出涵蓋該缺漏時段的 gap marker）
- [x] 6.2 實作 capture 指標輸出（coverage % = 有資料輪數 / 應有輪數、總 gap 分鐘、去重計數）（spec: Capture-Feasibility Metrics）（驗證：多日跑後輸出三項指標）

## 7. Documentation

- [x] 7.1 在 CLAUDE.md 記錄 logger canonical 儲存位置（mini-che 外接 NVMe 根路徑 + city / date / dim / serving 目錄結構，外接碟 volume 名待實機掛載後填入）與子系統說明（spec: Partitioned Parquet on Designated External Storage）（驗證：CLAUDE.md 含該章節、路徑與目錄結構明確）
- [x] 7.2 [P] 撰寫 logger 部署 README：mini-che 上的 launchd / cron 部署、輪頻、TDX 認證、外接碟掛載前提（spec: Continuous Dual-Signal Capture）（驗證：README 含可照做的部署步驟）

## 8. Capture-Feasibility 驗收

- [ ] 8.1 連續執行 ≥7 天 capture-feasibility 驗收：coverage 達門檻、gap 分鐘完整記錄、A2 去重抽樣人工核對正確、產出 Parquet 經 DuckDB 查詢且 schema 符合 spec（spec: Capture-Feasibility Metrics）（驗證：驗收報告含三項指標與 DuckDB 查詢結果）
