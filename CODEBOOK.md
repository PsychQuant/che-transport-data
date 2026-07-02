# CODEBOOK — che-transport-data 資料字典

本檔是全部資料產物的欄位級文件：Parquet lake（canonical）、weather lake、`warehouse.duckdb`（衍生）。改 schema 必須同步改這裡。

## 0. 全域約定

| 約定 | 內容 |
|------|------|
| 時區 | 所有 timestamp 為 **Asia/Taipei（+08:00）**；parquet 內為 TIMESTAMPTZ |
| 分區 | Hive 式：`city=<Taipei\|NewTaipei>/date=<YYYY-MM-DD>`（date = 服務日，台北時區）；weather 用 `county=<縣市>/date=` |
| Thin-fact | fact 只存自然鍵 + 量測 + 時間，**不重複名稱**；名稱/座標在 dimension，查詢時 join |
| Empty ≠ error | 某日/某城無檔 = 源頭當時無資料（見 gap marker），不是錯誤 |
| `source` 欄 | 資料來源 feed 代號：`A2`（RealTimeNearStop）/`A1`（RealTimeByFrequency）/`N1`（EstimatedTimeOfArrival） |
| 缺漏誠實 | logger 中斷不可回補；缺口記錄在 `gaps/gaps.jsonl`，不插值、不假裝連續 |
| 機密政策 | repo 為 public：**憑證永不進 git**（TDX creds 在 mini 本機 0600 檔；CWA key 在 keychain）；資料檔（parquet/duckdb）不進 git，只存 mini NVMe + 異地備份 |

## 1. Parquet lake（canonical，`/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet/`）

### 1.1 `arrival_event/`（A2，30s 輪詢，寫入前去重）

到站真值。logger 以 90 秒窗對 (plate, stop, direction) 去重；同一事件跨輪詢重報（`event_ts` 不變）仍可能殘留，warehouse 載入時以完整鍵再折一次。

| 欄位 | 型別 | 語意 |
|------|------|------|
| `plate` | VARCHAR | 車牌（vehicle 自然鍵） |
| `route_uid` | VARCHAR | TDX RouteUID（**聚合 sub-route**——正線/區間車共用，見 §3 bridge 註記） |
| `direction` | BIGINT | 0=去程 1=返程（TDX Direction） |
| `stop_uid` | VARCHAR | TDX StopUID |
| `stop_sequence` | BIGINT | 站序（**per sub-route**，同 route_uid 下不唯一） |
| `event_type` | BIGINT | TDX A2EventType：**0=離站、1=進站** |
| `gps_time` | TIMESTAMPTZ | 事件當下 GPS 時間（源頭提供） |
| `gps_lat`/`gps_lon` | DOUBLE | 事件座標（WGS84） |
| `captured_at` | TIMESTAMPTZ | logger 抓取時刻 |
| `event_ts` | TIMESTAMPTZ | 事件時間（去重錨點） |
| `source` | VARCHAR | 恆 `A2` |
| `city`/`date` | 分區鍵 | — |

### 1.2 `vehicle_position/`（A1，10s 輪詢，全量不去重）

即時車輛 GPS。**座標只在 A1**（A2/N1 不帶完整軌跡）。10s 對應實測 TDX GPS 更新率 ~15–20s。

| 欄位 | 型別 | 語意 |
|------|------|------|
| `plate` | VARCHAR | 車牌 |
| `route_uid`/`sub_route_uid` | VARCHAR | 路線／子路線（A1 有 sub_route，A2 沒有） |
| `direction` | BIGINT | 0/1 |
| `gps_lat`/`gps_lon` | DOUBLE | WGS84 |
| `speed` | BIGINT | km/h（源頭值，未清洗） |
| `azimuth` | BIGINT | 方位角（度） |
| `duty_status` | BIGINT | TDX DutyStatus：0=正常 1=出勤開始 2=出勤結束 |
| `bus_status` | BIGINT | TDX BusStatus：0=正常營運，其餘為異常碼（車禍/故障/…） |
| `gps_time` | TIMESTAMPTZ | GPS 定位時刻 |
| `captured_at` | TIMESTAMPTZ | 抓取時刻 |
| `source` | VARCHAR | 恆 `A1` |

> 分析清洗慣例（`analysis/spacetime.py`）：只取 `duty_status=1 AND bus_status=0`，投影丟 >200m 離線點。

### 1.3 `eta_snapshot/`（N1，120s 輪詢）

TDX 官方 ETA 的快照——預測基準線（我們的模型要贏的對象）。

| 欄位 | 型別 | 語意 |
|------|------|------|
| `route_uid`/`direction`/`stop_uid` | — | 預估對象 |
| `estimate_time_sec` | BIGINT | 預估到站秒數；NULL/負值=無預估 |
| `stop_status` | BIGINT | TDX StopStatus：0=正常 1=尚未發車 2=交管不停靠 3=末班已過 4=今日未營運 |
| `plate` | INTEGER* | **N1 幾乎不帶車牌**（全 NULL 欄被推斷成 INTEGER 的型別漂移；讀取請 `union_by_name=true` 並視為 VARCHAR/NULL） |
| `src_update_time` | TIMESTAMPTZ | 源頭更新時刻（可能含 epoch sentinel） |
| `captured_at` | TIMESTAMPTZ | 抓取時刻 |
| `source` | VARCHAR | 恆 `N1` |

### 1.4 `gaps/gaps.jsonl`（缺口紀錄）

每行一段不可回補的中斷：`{"gap_start": ISO8601, "gap_end": ISO8601, "duration_min": float}`。由 heartbeat 差值自動偵測；短於門檻（~1 分鐘）的中斷不記。

## 2. Weather lake（`/Volumes/mini-2TB-SSD/che-transport/weather/parquet/obs/`）

CWA `O-A0003-001` 自動站觀測，全台 ~362 站，10 分鐘一輪。**全欄位 VARCHAR 是刻意的 raw 捕捉**（源頭格式不穩定，cast 留到查詢期）；`-99`/`-990` 類值 = CWA 缺測代碼。

| 欄位 | 語意 |
|------|------|
| `station_id`/`station_name` | CWA 站號／站名 |
| `county` | 縣市（分區鍵） |
| `lat`/`lon` | 站座標 |
| `obs_time` | 觀測時刻 |
| `air_temp`/`precip`/`humidity`/`wind_speed`/`weather` | 氣溫℃／時雨量mm／相對濕度／風速 m/s／天氣描述 |
| `captured_at` | 抓取時刻 |

> 對齊慣例：以「離路線最近站」join，不是縣市粗對齊。

## 3. `warehouse.duckdb`（衍生分析庫，不進 git，可隨時重建）

位置 `…/bus-eta/warehouse/warehouse.duckdb`（**檔名勿用 `bus_eta`**——會與 schema 名撞 catalog ambiguity）。schema `bus_eta`。重建：`logger/warehouse/README.md`。

### Facts（**無 PK/合成鍵**——ART 索引常駐記憶體且不可 spill，億級表在 32GB 機器 OOM；唯一性由載入 dedup window + verify DISTINCT 比對守門）

| 表 | grain | 去重鍵（載入時） |
|----|-------|------------------|
| `fact_arrival_event` | 一筆=一次進/離站事件 | (city, plate, route_uid, direction, stop_uid, event_type, event_ts) |
| `fact_vehicle_position` | 一筆=一次 GPS 回報（不去重） | — |
| `fact_eta_snapshot` | 一筆=一次 ETA 預估快照 | (city, captured_at, route_uid, direction, stop_uid, plate) |

各 fact 附 `service_date`（分區替換鍵）；`warehouse_partition_load` 記錄每次載入的 bookkeeping（source/loaded 列數、時間）。

### Dimensions（SCD Type-2：`valid_from`/`valid_to`/`is_current`；`attr_hash='__stub__'` = 從 fact 建的佔位列，等 TDX staging hydration）

| 表 | 自然鍵 | 屬性 |
|----|--------|------|
| `dim_route` | (city, route_uid) | route_id、中英文名、起訖站、operator_id |
| `dim_stop` | (city, stop_uid) | stop_id、中英文名、lat/lon |
| `dim_vehicle` | (plate) | operator_id、vehicle_type |
| `bridge_route_stop` | (city, route_uid, direction, stop_sequence, **stop_uid**) | — |

> **bridge 鍵含 stop_uid 的原因（FD 否證）**：TDX `route_uid` 聚合 sub-route、`stop_sequence` 是 per sub-route——實測 5.2% 的 (route,dir,seq) 對應 2–6 個站，「seq 決定 stop」不成立。A2 未採集 SubRouteUID；若未來補收，bridge 可 re-key。

### Staging（`stg_tdx_*_current`，全量刷新）

由 `logger/warehouse/fetch_tdx_staging.py` 從 TDX 靜態 API（Route/Stop/StopOfRoute/Vehicle）填入，再跑 `--mode scd2` hydrate 維度。

### Views

| view | 語意 |
|------|------|
| `v_arrival_event_resolved` | fact × dim 的 **point-in-time join**（`event_ts BETWEEN valid_from AND valid_to`），出站名/路線名/座標 |
| `v_eta_snapshot_resolved` | 同上，for ETA 快照 |

## 4. 已知資料品質事項（誠實清單）

1. **06-13、06-14 無資料**（源頭中斷，gap 已記錄）；06-10 有 37.2h TDX 憑證事故缺口。
2. A2 的 `route_uid` 聚合 sub-route（§3 bridge 註記）；分析要分子路線時用 A1 的 `sub_route_uid`。
3. N1 `plate` 型別漂移（§1.3）。
4. `src_update_time` 可能含 epoch sentinel（2000-01-01+08），非真實時間。
5. weather 全 VARCHAR + CWA 缺測代碼（§2），cast 前先濾。
