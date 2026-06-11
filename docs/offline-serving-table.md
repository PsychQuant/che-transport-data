# Offline Serving Table — 可行性分析（hard-coded 預測表）

> 問題（2026-06-12）：「如果所有臺北的路線都分析完了，能不能把路線預測 hard-code
> 進一個 app，讓它瞬間知道什麼時候在什麼站有哪台車？」
>
> 結論：**一半可行、一半物理上不可行**。可行的那半正是 Phase 2 `serving/`
> 預算表（P50/P80 → `bus_eta_predict`）的設計；不可行的那半不是資料量問題，
> 是班距制 + bunching 的隨機性本質。

## 實測依據（2026-06-12，logger 開跑 ~2 天）

資料：bus-eta logger A2 `arrival_event`（整城 bulk，**395 條臺北路線、1,399,110 筆
到站**——「所有路線」的前提自動成立，無需逐路線另收）。

對每個 cell =（route × direction × stop × hour, n≥5）計算相鄰到站間隔（gap，
取 2–90 min 合理範圍）：

| 量 | 值 |
|----|-----|
| 可建表 cells | 47,056 |
| 中位班距 | **13.3 min** |
| cell 內 gap 的 P10–P90 散布（中位） | **16.0 min — 比班距還大** |
| 規律 cell（散布 < 0.6×班距） | **9.8%** |
| 混亂 cell（散布 ≥ 一整個班距） | **65.3%** |

重現：DuckDB over `arrival_event`，`lag(gps_time) OVER (PARTITION BY route_uid,
direction, stop_uid ORDER BY gps_time)` 取 gap，按 hour 分 cell 聚合
median / quantile（見 `analysis/spine.sql` 的 view 基底）。

**解讀**：在「只知道時段」的條件下，臺北公車到站近乎**無記憶**——歷史能告訴你
「典型班距 13 分、等待 P80 ≈ X 分」，但無法告訴你「下一班 14:42 到」，因為同條件
下實際到站時刻的散布超過一個班距。根因：臺北公車是**班距制**（多數路線無固定
發車時刻）+ **bunching 正回饋**（晚的車載更多客→更晚）。這不是資料不夠——
再收 100 天，**散布本身不會縮**（只是把散布量得更準）。Hard-code 能凍結的是
「分布」，不是「實現」。

### 量測 caveats（誠實揭露）

- 僅 ~2 天有效資料：hour-cell 混了少數天的樣本，散布估計本身仍粗；但散布的
  **主成分是 within-hour 的班距不規律（bunching）**，不是跨日變異，故結論方向穩。
- A2 30s 輪詢可能漏抓少數到站 → 偶發雙倍 gap 輕微膨脹散布（已用 2–90 min 範圍
  過濾極端值）。

## 三層裁決

| 想 hard-code 的東西 | 可行？ | 理由 |
|---|---|---|
| 「這站這時段**等多久**」（P50/P80 等待分布） | ✅ 完全可行且有價值 | 47K cells 已可建表；全表 MB 級（route 剖面 + 站偏移分解後更小）；app 內 O(1) 查詢、離線可用 |
| 「**幾點幾分**有車到站」（點預測） | ❌ 物理不可行 | 65% cell 散布 ≥ 班距 → 點預測典型誤差 ±半個班距起跳 |
| 「有**哪台車**（車牌）」 | ❌ 完全不可行 | 車輛派班每日洗牌（司機班表／保養），離線表凍結不了 |

## 正確形態：prior + overlay（不是取代即時）

```
離線 hard-coded 表 ＝ prior     ：瞬間、零 API、離線（捷運隧道）可用
        ＋
即時 A2/N1（在線時）＝ overlay  ：把「13 分內會有一班」修正成「3 分後那班」
```

即 NAVITIME / Google 的 schedule-prior + realtime-overlay 架構。對本 repo 的
直接含義：

1. **這就是 `serving/` 預算表**（儲存設計既有的 Phase 2 規劃）：
   per (route, dir, stop, time-bin, day-type) 的 P50/P80 等待 + run-time 剖面。
2. **Rush 的 frequency 模型直接升級**：現在捷運／公車 `source: frequency` 用裸
   `E[wait]=headway/2`；serving 表把它升級為**實證的、分時段的** P50/P80——
   現有 routing tools 立即受益，且維持「誠實標示資料來源」的原則。
3. 尺寸估算：~500 路線 × 2 向 × ~50 站 × 72 時段 × 3 day-type ≈ 10⁷ cells 上限，
   經「per-route 剖面 + per-stop 偏移」分解與量化後 **MB 級**，可隨 app 出貨。

## 資料火候

- 穩定的 per-hour cell 需要 **3–4 週**（涵蓋星期 × 天氣組合；天氣 covariate 由
  weather logger 對齊）。
- 7 天 capture-feasibility run（至 ~2026-06-18）先驗證 pipeline；表的火候讓
  logger 繼續跑即得。

## 相關文件

- 預測 metric（一律用秒）與回報慣例：`docs/bus-eta-prediction.md`
- 對齊 marts（trajectory / prediction_error）：`analysis/spine.sql`
- 可預測性天花板（bias→0 但 variance 有地板）：`docs/bus-eta-prediction.md` §4
