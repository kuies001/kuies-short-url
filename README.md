# kuies.tw 短網址服務

自架短網址服務，對外網域：`https://u.kuies.tw`。

## 位置

- 專案：`/Users/example/.hermes/experiments/kuies-short-url`
- 資料庫：`data/shorturls.sqlite3`
- launchd：`~/Library/LaunchAgents/com.kuies.short-url.plist`
- 服務 Port：`8787`
- 短網址頁面：`https://u.kuies.tw/surl`
- GKD／icash Pay 教學短網址：`https://u.kuies.tw/gkd`（Android 限定，提供本地應用規則、安全邊界、測試與停用方式）

## URLCheck 社群追蹤清理規則

- ClearURLs v2 完整規則目錄：`https://u.kuies.tw/ucl`
- SHA-256 驗證檔：`https://u.kuies.tw/uch`
- 規則保留官方 ClearURLs 的全部 provider，並補上 Threads `xmt`／`slof`、Instagram `igsh`／`igshid`、Facebook `fbclid`／`mibextid`／`rdid`／`share_url`。
- 內容識別參數（例如 Facebook `story_fbid`／`id`、影片 `v`、Instagram `img_index`、留言 `comment_id`）不會刪除。
- `/share/.../` 是平台包裝路徑而非 query 追蹤參數，不能由 ClearURLs 規則直接還原。
- URLCheck「模式檢查器」完整設定：`https://u.kuies.tw/ucp`。貼入該模組的 JSON 編輯器後，Facebook／Threads `/share/.../` 會自動改寫到安全解析端點 `https://u.kuies.tw/sr?url=...`。
- URLCheck「檢查狀態」必須開啟「自動前往重新導向」；解析端點回 HTTP 302 後，URLCheck 才會把畫面網址換成真正貼文網址，接著交給網址清理器刪除 query 追蹤參數。
- 解析端點只接受已列入白名單的 HTTPS Facebook／Threads 分享網域與路徑；解析失敗回 HTTP 422，不猜測網址，也不充當任意開放重新導向器。
- 更新腳本：`scripts/update_urlcheck_social_rules.py`。成功時保持靜默；抓取或驗證失敗時以非零狀態結束。

## 網頁介面

開啟：

```text
https://u.kuies.tw/surl
```

外網使用者可用：

- 縮短網址
- 自訂短碼，建議 ≤5 字元
- 自動短碼不會產生 `_` 字元
- 成功後只顯示本次產生的短網址
- 複製網址按鈕
- 可勾選「移除追蹤參數」；目前支援 Threads / Threads.net 移除 `xmt`，以及 Instagram / IG 移除 `utm_source` 與 `igsh` 後再縮網址
- 公開頁面有基本反爬蟲檢查，且短時間內不可多次建立短網址，以降低濫用/攻擊風險
- 頁面下方提供 Chrome 擴充下載連結

外網使用者不可用：

- 不顯示管理金鑰登入欄位
- 不顯示已建立短網址清單
- 不可進入管理介面

內網使用者可用：

- 看到「管理欄位」
- 輸入 `.env` 裡的管理金鑰後進入管理介面
- 管理介面可建立短網址
- 管理介面可查看最近 200 筆短網址與點擊數
- 管理介面可刪除短網址

注意：如果 `.env` 的管理金鑰有變更，需要重啟服務才會生效：

```bash
launchctl kickstart -k gui/$(id -u)/com.kuies.short-url
```

## 內外網判斷

服務會根據 NAS 反向代理傳來的 `X-Forwarded-For` 判斷來源 IP：

- 私有 IP / loopback / link-local：視為內網
- 公開 IP：視為外網

NAS 反向代理建議覆寫或正確傳遞 `X-Forwarded-For`，不要讓外部使用者自訂的 `X-Forwarded-For` 原樣穿透。此服務端會在多段 `X-Forwarded-For` 中只要看到公開 IP 就視為外網，以降低偽造內網 IP 的風險。

## 社群預覽架構

- 支援 Meta／Messenger、Telegram、Discord、Slack、X/Twitter、LinkedIn 與 WhatsApp 預覽爬蟲；爬蟲取得第一方 OG 頁，真人仍直接 302 到原網址。
- Threads／Instagram 新短網址在 API 回傳前會先同步完成首次預覽暖機，把標題、摘要與圖片存進 SQLite／本機快取；避免擴充剛複製短網址、使用者立刻分享時，通訊軟體搶先把「Threads 分享連結」永久快取。首次暖機後的定期更新仍採背景執行，社群爬蟲來訪時只讀快取，不同步等待 Threads。
- 若首次來源抓取失敗，fallback 標題使用可辨識來源帳號的 `Threads 貼文｜@帳號`／`Instagram 貼文｜@帳號`，不再輸出模糊的「分享連結」。公開 API 回應包含 `preview_status`、`preview_title`、`preview_description` 與 `preview_updated_at`，方便擴充或診斷工具確認預覽是否就緒。
- 預覽資料成功時每 7 天背景更新；來源為登入牆、刪除或暫時失敗時，每小時背景重試。
- 每個短碼都有獨立 fallback 圖片網址 `/preview-image/<code>.png`，避免社群平台把共用圖片的一次失敗快取到所有短網址。
- 圖片讀取同時搜尋外接碟與 `data/preview-images`，避免外接碟掛載狀態改變造成舊快取突然消失。
- 若 Threads 原始貼文本身已刪除、設為不公開或回傳 `invalid_post`，服務只能顯示第一方通用預覽圖，無法復原原貼文圖片。

## Chrome 擴充

下載：

```text
https://u.kuies.tw/downloads/threads-link-cleaner.zip
```

安裝方式：

1. 下載 zip。
2. 解壓縮。
3. 開啟 Chrome：`chrome://extensions/`。
4. 開啟右上角「開發人員模式」。
5. 點「載入未封裝項目」。
6. 選擇解壓縮後的 `threads-link-cleaner` 資料夾。

功能：

- 目前版本：`1.7.3`。
- 點開擴充後有一個自動模式開關與兩個手動按鈕：
  - `自動去除追蹤並縮短`：開啟後，在 Threads、Facebook 與 IG 點「複製連結」時，會攔截頁面剪貼簿寫入；Threads 新版 `/share/<token>` 與 Facebook `/share/p/<token>` 等包裝網址會先解析成真正貼文網址，再移除 `xmt`、`slof`、`fbclid`、`mibextid`、`rdid`、`share_url` 等追蹤參數、建立 `u.kuies.tw` 短網址並寫回剪貼簿。
  - 關閉自動模式：保持原先手動功能，不主動攔截 Threads / Facebook / IG 複製連結。
  - `更新手動/自動狀態`：手動把目前 popup 的自動模式設定同步到當前分頁；若剛切換後頁面仍沿用舊狀態，可按此按鈕或重新整理頁面。
  - `縮短網址`：讀取目前瀏覽器 active tab 的網址列，直接建立 `u.kuies.tw` 短網址，不移除任何參數，並把短網址寫回剪貼簿。
  - `縮短+去除追蹤`：讀取剪貼簿第一個 `http://` 或 `https://` 網址，建立短網址前先移除已知社群追蹤參數，並把短網址寫回剪貼簿。
- 目前追蹤參數清理規則：
  - Threads：以 `threads.com` 為主要支援網域，並兼容舊的 `threads.net`；解析 `/share/<token>` 並移除 `xmt`、`slof`。
  - Facebook：支援 `facebook.com`、`www.facebook.com` 與 `m.facebook.com`；解析 `/share/p/`、`/share/r/`、`/share/v/` 等包裝網址，保留貼文識別參數並移除 `fbclid`、`mibextid`、`rdid`、`share_url` 等追蹤資訊。
  - Instagram：支援 `instagram.com` / `www.instagram.com`；移除 `igsh`、`utm_source`、`utm_medium`、`utm_campaign`、`utm_content`、`utm_term`、`utm_id`。
- 自動模式使用頁面注入攔截器，攔截 `Navigator.prototype.clipboard.writeText/write`、`document.execCommand('copy')` 與一般 copy event，以涵蓋 Threads、Facebook 與 Instagram 的不同複製方式；頁面攔截器預設關閉，必須收到擴充同步狀態後才會啟用，避免關閉自動模式後仍自動縮短。
- `writeText/write` 會先透過 page world ↔ content script request/response 橋接取得短網址，再執行唯一一次最終剪貼簿寫入，避免首次複製時「清理後原始網址」與「短網址」兩次非同步寫入互相覆蓋。
- 自動複製會傳送 `fast_response: true`：伺服器先立即回傳短碼，再於背景完成 Threads／Instagram 預覽暖機，避免等待 2～13 秒的社群 metadata 抓取才出現「已複製」。快速模式每個 IP／User-Agent 每分鐘可用 30 次，一般公開表單仍維持每分鐘 5 次；每日 100 次與既有防濫用規則不變。
- 若縮網址 API 暫時失敗或 2.5 秒內沒有回應，剪貼簿會降級保留已去除追蹤參數的原始連結。

## 命令列建立短網址

建議使用輔助腳本，腳本會自動讀取 `.env` 的管理金鑰。

自訂短碼：

```bash
/Users/example/.hermes/experiments/kuies-short-url/add-url.sh "https://example.com/very/long/url" demo
```

自動短碼：

```bash
/Users/example/.hermes/experiments/kuies-short-url/add-url.sh "https://example.com/very/long/url"
```

回傳會包含：

```json
{
  "code": "demo",
  "short_url": "https://u.kuies.tw/demo",
  "target_url": "https://example.com/very/long/url"
}
```

## API

- `POST /api/urls`：建立短網址，需要管理金鑰
- `GET /api/urls`：列出短網址，需要管理金鑰
- `DELETE /api/urls/<短碼>`：刪除短網址，需要管理金鑰
- `POST /api/public/shorten`：公開 JSON 縮網址 API，不需要管理金鑰，Chrome 擴充使用；支援 `clean_threads_xmt: true` 與 `clean_tracking: true`，外部使用者有短時間使用次數限制
- `POST /shorten`：公開縮網址表單使用，不需要管理金鑰；含表單 token、honeypot 欄位與短時間使用次數限制
- `GET /downloads/threads-link-cleaner.zip`：下載 Chrome 擴充 zip
- `GET /<短碼>`：302 跳轉到長網址
- `GET /healthz`：健康檢查
- `GET /surl`：短網址頁面與內網管理入口
- `GET /admin`：已移除，回傳 404

## 郵件警示

建立短網址成功後會檢查資料量，符合以下任一條件時寄信通知管理者：

- 5 分鐘內新增達 100 筆短網址。
- 短網址總筆數達 1000 筆。

預設通知收件人：`alerts@example.com`；寄件人：`short-url@example.com`。可用環境變數調整：

- `SHORT_ALERT_EMAIL`：收件人，預設 `alerts@example.com`。
- `SHORT_ALERT_FROM`：寄件人，預設 `short-url@example.com`。
- `SHORT_ALERT_SENDMAIL`：sendmail 路徑，預設 `/usr/sbin/sendmail`。

警示狀態會存在 SQLite 的 `alert_state` 表，避免總筆數達 1000 後每次新增都重複寄信；5 分鐘新增 100 筆警示有 1 小時冷卻時間。

## 管理服務

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kuies.short-url.plist
launchctl kickstart -k gui/$(id -u)/com.kuies.short-url
launchctl bootout gui/$(id -u)/com.kuies.short-url
launchctl print gui/$(id -u)/com.kuies.short-url
```

## 健康檢查

```bash
curl -i http://127.0.0.1:8787/healthz
curl -i https://u.kuies.tw/healthz
```
