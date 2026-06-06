# Feasibility Spike Report — bus-eta-logger Task 1.1

**Measured**: 2026-06-04 ~14:04 Asia/Taipei（**離峰下午**；尖峰時段記錄數會更高，請於實作時再取尖峰樣本校準）
**Method**: 直打 TDX raw（OAuth client_credentials → per-city bulk endpoints），憑證讀自 keychain `che-transport-tdx`（未外洩）。

## 1. Per-city bulk 記錄數（單輪 = 一次呼叫）

| City | Feed (endpoint) | 記錄數 | 回應大小 | `$top` 分頁? |
|------|------|------|------|------|
| Taipei | A2 `Bus/RealTimeNearStop/City/Taipei` | 1,511 | 938 KB | **無**（default == top100k）|
| Taipei | N1 `Bus/EstimatedTimeOfArrival/City/Taipei` | 28,784 | 9.3 MB | **無**（default == top100k）|
| NewTaipei | A2 `Bus/RealTimeNearStop/City/NewTaipei` | 1,014 | 626 KB | **無** |
| NewTaipei | N1 `Bus/EstimatedTimeOfArrival/City/NewTaipei` | 32,901 | 10.5 MB | **無** |

**關鍵發現**：
1. ✅ **per-city A2/N1 bulk 端點可用**（http 200）——回答 diagnose 留下的「per-city A2 形式是否可用」：**是**。
2. ✅ **不需要 `$top/$skip` 分頁**：TDX 一次回傳全部（Taipei A2/N1 的 default 與 `$top=100000` 筆數相同）。→ diagnose 列的最大風險（N1 分頁逼爆請求數）**不存在**。
3. 大臺北單輪 = **4 個請求**（Taipei A2 + Taipei N1 + NewTaipei A2 + NewTaipei N1），各 1 次、無分頁。

## 2. Rate limit 實測

- TDX free tier 文件值 **50 req/min**。
- 量測時**連續爆衝 ~8 calls / 15s**（每城每 feed 各打 default + top100k 兩次）即觸發 **HTTP 429 "API rate limit exceeded"**（NewTaipei N1 那筆被擋）。
- 推論：**爆衝容忍度比 50/min 穩態低**（可能 burst limiter / 大回應佔權重 / 與其他近期呼叫共用配額）。logger 必須**把每輪請求攤開、不要同時打**，並處理 429（設計已含：單次退避重試→二次跳過）。

## 3. 請求預算（選定輪頻，證明 ≤ 限額）

採分頻（A2 真值高頻、N1 baseline 低頻，N1 量大）：

| Feed | 輪頻 | req/min（2 城）|
|------|------|------|
| A2 RealTimeNearStop | 30s | 2 城 ÷ 30s = **4/min** |
| N1 EstimatedTimeOfArrival | 120s | 2 城 ÷ 120s = **1/min** |
| **合計** | | **≈ 5 req/min** ✅ 遠低於 50/min |

- 即使較激進（A2 20s / N1 60s）= 6 + 2 = **8/min**，仍安全。
- **務必**：每輪 4 個請求**間隔送出**（勿同時爆衝）；保留 429 退避。

## 4. 儲存量級（粗估，供 schema/retention 參考）

- **A2（真值）**：~2,525 筆/輪、~1.5MB raw JSON。多為「同車同站重複回報」→ **去重後**只剩實際到站事件（每車每站每趟一次）→ 落地**很小**（dedup 是關鍵）。
- **N1（baseline）**：~61,685 筆/輪、~20MB raw JSON。**這是儲存大宗**：120s 輪頻 ≈ 14GB/天 raw → Parquet 欄式壓縮後估 ~0.5–1.5GB/天。2TB NVMe 可撐數年；若有壓力可調 N1 至 300s 或抽樣。

## 5. 結論

**大臺北（Taipei + NewTaipei）全公車捕捉在 TDX free tier 內可行**：每輪 4 請求、無分頁、選定 A2 30s / N1 120s ≈ 5 req/min（≤ 50/min，有大量餘裕）。唯一注意：請求需攤開避免爆衝 + 處理 429。N1 為儲存大宗，靠 A2 去重 + N1 低頻/可調控制總量。

> 後續實作（task 3 ingestion）應沿用此預算；建議部署初期跑一段「受控低速」確認穩態不觸 429，並補一份尖峰時段記錄數樣本。
