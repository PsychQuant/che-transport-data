# CLAUDE.md — che-transport-data

台灣大眾運輸資料平台（採集 → Parquet → DuckDB → 分析）。2026-07 自 `PsychQuant/rush` 分拆（歷史完整保留）；MCP 工具面在 rush，本 repo 專責資料。

## Repo 定位 — PUBLIC，掛載為 rush 的 `repos/` submodule

**現行決策（2026-07-02 晚，取代同日稍早的 standalone-only 定位）**：本 repo 轉 **public**，並以 submodule 掛在 `PsychQuant/rush` 的 `repos/che-transport-data`（比照 mac-benchmark ↔ macllm-roofline 的 repos/ pattern）。稍早反對 submodule 的兩個理由（public×private 弄壞外部 clone、`.gitmodules` 洩漏私有路徑）在轉 public 後即不成立，使用者裁定掛載。

- **公開的前提**（轉 public 前已全歷史機密掃描通過）：憑證永不進 git（TDX creds = mini 本機 0600 檔、CWA key = keychain）；資料檔（parquet/duckdb）不進 git。欄位級文件見 `CODEBOOK.md`。
- 主要工作副本 = rush checkout 內的 `repos/che-transport-data`（submodule）；**mini 部署仍是獨立直接 clone**（`~/che-transport-data`，單層 `git pull`，不經 superproject）。
- 不掛 che-mcps（MCP 專用傘，性質不合，維持原判）。

## Spectra

本 repo 沿用 Spectra SDD：specs 在 `openspec/specs/`、changes 在 `openspec/changes/`（`bus-eta-logger` change 隨拆分搬入，僅剩 task 8.1 七天驗收未結）。

## Rate limit（與 rush 共用 TDX 帳號）

TDX 2026-06 訂閱制——基礎(免費) 5 次/分／銅級(200元/月) 5 次/秒／銀 10/秒／金 30/秒／白金 50/秒。本帳號已訂**銅級**（bus-eta logger ~17 req/分 需 ≥銅級；基礎裝不下）。

## Development

```bash
python3 -m venv logger/.venv
logger/.venv/bin/pip install -r logger/requirements.txt
logger/.venv/bin/python -m pytest logger/tests -q     # 34+ tests
```

## Bus ETA Logger — 資料儲存位置（mini-che 外接 NVMe）

> 對應 change `openspec/changes/bus-eta-logger`（Stage 3+ 資料採集層）。logger 為獨立 **Python** 常駐程序，跑在 **mini-che（PsychQuantMini，che830621 帳號，常開）**，**與 rush read-only MCP 分離**。TDX 公車動態僅滾動保留 ~2h、無任何現成歷史來源（已查證），故須自記——源頭即丟，誰先記誰獨有。

**Canonical 儲存根**（mini-che 外接 USB4 NVMe：PROBOX 盒 + Kingston NV3 2TB）：

```
/Volumes/mini-2TB-SSD/che-transport/bus-eta/
├── parquet/                                                       # fact 表（BCNF thin-fact：只存 FK + 量測 + 時間）
│   ├── arrival_event/city=<code>/date=<YYYY-MM-DD>/*.parquet     #   A2 去重後到站事件（到站真值）
│   ├── vehicle_position/city=<code>/date=<YYYY-MM-DD>/*.parquet  #   A1 即時車輛 GPS 位置（全量，不去重）
│   └── eta_snapshot/city=<code>/date=<YYYY-MM-DD>/*.parquet      #   N1 ETA baseline 對照
├── dim/                                                           # SCD Type-2 dimension（route/stop/vehicle/route-stop bridge；valid_from/valid_to/is_current）
├── gaps/                                                          # gap marker（logger 中斷的不可回補缺漏時段）
├── serving/                                                       # 預算表（Phase 2：P50/P80 → bus_eta_predict）
└── warehouse/                                                     # warehouse.duckdb（logger/warehouse/ 建置的持久化分析庫；檔名勿用 bus_eta——會與 schema 名撞 catalog ambiguity）
```

- **Volume 名 = `mini-2TB-SSD`**（Kingston NV3 2TB；已掛載於 `/Volumes/mini-2TB-SSD`，`diskutil` 報 PCI-Express、Removable: Fixed）。
- **掛載守衛**：碟未掛載時 logger **拒絕寫入、不可 fallback 到系統碟（256G）**。
- 查詢引擎 = DuckDB；分析 = SSH 進 mini-che 在地跑或 rsync Parquet／單檔 `warehouse.duckdb` 回筆電（**勿隔 SMB 即時查**，延遲會咬）。
- **對齊分析**：`analysis/spine.sql` 定義 DuckDB views（a1/a2/n1/arrivals）+ ASOF marts：`trajectory(t0,t1,step_sec)`（車軌跡，A1 位置前向填）、`prediction_error`（每筆到站 vs N1 預測的誤差；N1 無 plate 故 join 在 route/dir/stop）。mini 無 duckdb CLI → 用 logger venv 的 python duckdb（已裝 `pytz` 供 timestamptz 輸出）：`con.execute(open('analysis/spine.sql').read())`。
- **路線視覺化**：`analysis/marey.py <route>`（站序 Marey 時空圖，`--normalize` 出 run-time profile）、`analysis/spacetime.py <route>`（A1 GPS 投影到路線 Shape 的**真距離** distance-time，slope=真 km/h；含清洗：濾 `duty_status=1`&`bus_status=0`→投影丟 >200m 離線→切趟→覆蓋率≥80%&前進率≥80%，`--keep-anomalies` 灰線疊示被丟趟）、`analysis/features_gps.py <route>`（段速熱圖=內生壅塞圖 + 前車 headway covariate）。產生的 PNG 在 `analysis/output/`（gitignored）。
- **採集 feeds（3 條，各自節奏）**：`A2`(30s)→`arrival_event`（到站真值，去重）／`A1`(10s)→`vehicle_position`（即時車輛 GPS 位置，全量不去重；A2/N1 都不帶座標，位置只在 A1）／`N1`(120s)→`eta_snapshot`（ETA 預測基準）。A1 取 10s 是對應實測 TDX GPS 更新率 ~15–20s（再細是重複、源頭沒那麼細）。
- 涵蓋範圍：大臺北（Taipei + NewTaipei）。異地備份：Dropbox / R2。

### 部署現況（2026-06-09 起跑）

logger 已部署並運行於 mini-che。部署踩過三道 macOS 關卡，操作需求記錄如下：

| 項目 | 值／路徑 | 為什麼 |
|------|----------|--------|
| launchd agent | `~/Library/LaunchAgents/tw.psychquant.bus-eta-logger.plist`，GUI domain 載入（`launchctl bootstrap gui/$(id -u) <plist>`）| RunAtLoad + KeepAlive 常駐；env 帶 `BUS_ETA_VOLUME=/Volumes/mini-2TB-SSD` + `BUS_ETA_DATA_ROOT=.../parquet`。plist 版本控制於本 repo `logger/`，改 plist 後須 bootout+bootstrap（kickstart 不重讀）|
| TDX 憑證 | **600 本機檔** `~/.config/bus-eta-logger/tdx.json`（`{client_id, client_secret}`），**非 keychain** | launchd 讀 keychain 會卡授權對話框（classic ACL + partition list 兩道閘都認 che-keychain、不認 launchd 的 python）。改檔案 daemon 讀取永不跳框。poller `_load_creds()` 優先讀此檔、fallback keychain；檔不進 git／不上 NVMe |
| Full Disk Access | 授 FDA 給 `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9` | macOS TCC 擋 launchd 的 python 寫 `/Volumes` 外接卷宗（EPERM）；ssh 能寫（sshd 已授權）但 launchd 不行，須在「系統設定 → 隱私權 → 完整取用磁碟」加該 python。venv 建立要用 `/Library/Developer/CommandLineTools/usr/bin/python3`（symlink 鏈最終解到 FDA 授權的 framework binary）|

- **程式碼位置（mini）**：`~/che-transport-data/` 的 git checkout（2026-07 起；之前為鬆散複製 `~/bus-eta-logger/`＋`~/weather-logger/`，已歸檔 `~/archive-pre-split/`）。部署更新 = `git -C ~/che-transport-data pull` ＋ 需要時 bootout+bootstrap。
- **2026-06-10 事故**：TDX 端憑證失效（舊免費方案落日）→ 02:21 起 token 400、poller crash-loop 13h+。已修：startup/refresh token 失敗改 60s 重試（**每次重讀憑證檔**，換 key 免重啟自癒）、cycle 包非致命護欄；缺口由 gap marker 誠實記錄（37.2h）。06-11 15:35 訂閱銅級後自癒復跑。
- **重啟 agent**（plist 未變時）：`ssh mini-che 'launchctl kickstart -k gui/$(id -u)/tw.psychquant.bus-eta-logger'`
- **看狀態**：`ssh mini-che 'launchctl list | grep bus-eta; tail ~/Library/Logs/bus-eta-logger.err.log'`
- **查資料量**：`ssh mini-che 'find /Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet -name "*.parquet" | wc -l'`

### 天氣 logger（第二個 collector，2026-06-12 起跑）

`weather-logger/`（獨立常駐，同 mini-che）：CWA `O-A0003-001` 自動氣象站觀測（雨量/溫度/濕度/風）**全台 ~362 站**，每 10 分鐘 → `/Volumes/mini-2TB-SSD/che-transport/weather/parquet/obs/county=<>/date=<>/`。為 ETA 的天氣 covariate；分析時以「離路線最近站」對齊（非 coarse 縣市）。launchd agent `tw.psychquant.weather-logger`（GUI domain、reuse bus-eta venv + 已授 FDA 的 python）。

- **憑證在 keychain**（非檔案）：`che-keychain set --daemon`（allow-all ACL）**＋** `security set-generic-password-partition-list -S apple-tool:,apple: -s che-weather-cwa -a api_key`（partition gate）兩道齊開，launchd 才免提示讀。這是「launchd 讀 keychain」的驗證配方——`--daemon` 只開 ACL、partition-list 要登入密碼另設（`SecAccess` API 設不了 partition）。`_load_key` keychain-first（5s timeout）→ 檔案 → env fallback。
- **重啟**（plist 未變時）：`ssh mini-che 'launchctl kickstart -k gui/$(id -u)/tw.psychquant.weather-logger'`
- v2（未做）：F-D0047 鄉鎮預報（含 issue-time 的無洩漏 predict-time covariate）

## DuckDB Warehouse（`logger/warehouse/`）

Parquet lake 為 canonical、`warehouse.duckdb` 為可攜分析快照（thin fact 原生複製 + SCD2 dims + point-in-time resolved views）。主庫建在 mini（data gravity）、需要時 rsync 單檔回筆電。runner：`run_warehouse_sql.py`，modes = `compat`／`bootstrap`／`incremental`（date/city 分區整批替換、冪等）／`verify`／`scd2`。**尚未實跑驗證**——首跑順序 compat → 單日 bootstrap → verify。詳見 `logger/warehouse/README.md` 與 issue tracker。
