# Chrome Web Store 上架資料：kuies.tw Short URL

## 基本資料

名稱：kuies.tw Short URL

一句話說明：在 Threads 與 Instagram 複製連結時，自動移除追蹤參數並縮成 u.kuies.tw 短網址。

分類建議：Productivity

語言建議：繁體中文

官方網站：https://u.kuies.tw/surl

隱私權政策：https://u.kuies.tw/privacy/threads-link-cleaner

## 詳細描述

kuies.tw Short URL 是一個輕量的 Chrome 擴充功能，可協助你更乾淨地分享 Threads 與 Instagram 連結。

主要功能：

- 一鍵縮短目前分頁網址，產生 u.kuies.tw 短網址。
- 從剪貼簿讀取第一個網址，移除追蹤參數後縮短。
- 在 Threads 與 Instagram 點選「複製連結」時，自動移除常見追蹤參數並縮成短網址。
- 支援手動模式與自動模式切換。
- 若縮網址 API 暫時失敗，仍盡量保留已去除追蹤參數的原始連結。

支援清理的追蹤參數：

- Threads / Threads.net：xmt
- Instagram：igsh、utm_source、utm_medium、utm_campaign、utm_content、utm_term、utm_id

本擴充的單一目的，是協助使用者在分享社群連結時移除不必要的追蹤參數，並建立短網址。所有縮網址請求只送到 https://u.kuies.tw。

## 單一用途說明

在使用者主動縮短網址，或在 Threads／Instagram 複製分享連結時，移除常見追蹤參數並建立 u.kuies.tw 短網址。

## 權限理由

activeTab：使用者點擊擴充 popup 的「縮短網址」按鈕時，讀取目前作用中分頁的網址以建立短網址。

clipboardRead：使用者點擊「縮短+去除追蹤」按鈕時，從剪貼簿讀取第一個網址以清理追蹤參數並縮短。

clipboardWrite：建立短網址後，將短網址寫回剪貼簿，讓使用者可以直接貼上分享。

storage：儲存「自動去除追蹤並縮短」開關狀態，讓設定在瀏覽器工作階段之間保留。

host_permissions：https://u.kuies.tw/* 用於呼叫公開縮網址 API。Threads / Instagram 網域權限用於在支援網站載入內容腳本，偵測使用者複製分享連結並移除追蹤參數。

## 遠端程式碼聲明

No, I am not using remote code.

補充說明：擴充所有執行程式碼都包含在 ZIP 套件內。擴充會呼叫 https://u.kuies.tw/api/public/shorten 以建立短網址，但不從遠端下載或執行 JavaScript。

## 資料使用聲明建議

會處理的資料類型：Website content 或 Web browsing activity 相關的網址資料，僅限使用者主動縮短或在支援網站複製的 URL。

用途：App functionality。

不出售資料、不用於廣告、不轉移給第三方、不用於與核心功能無關的目的。

## 測試說明給審核員

1. 安裝擴充後開啟任意 http 或 https 網頁。
2. 點擊工具列中的 kuies.tw Short URL 圖示。
3. 點擊「縮短網址」，應會建立 u.kuies.tw 短網址並寫入剪貼簿。
4. 複製一個 Threads 或 Instagram 分享連結後，點擊「縮短+去除追蹤」，應會移除追蹤參數並建立短網址。
5. 在 popup 中可切換「自動去除追蹤並縮短」。啟用後，在 Threads 或 Instagram 頁面點擊複製分享連結時，擴充會嘗試清理追蹤參數並將短網址寫回剪貼簿。

不需要測試帳號。若社群網站介面限制複製功能，可使用手動「縮短+去除追蹤」按鈕測試。

## 素材檔案

- 擴充上傳 ZIP：static/downloads/threads-link-cleaner-cws.zip（目前版本 1.7.4）
- 小型宣傳圖：store-assets/promo-small-440x280.png
- 截圖一：store-assets/screenshot-1280x800.png
- 截圖二：store-assets/screenshot-clean-share-1280x800.png
- 隱私權政策文字備份：store-assets/privacy-policy.md
