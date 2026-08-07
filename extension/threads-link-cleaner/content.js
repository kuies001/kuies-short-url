// kuies.tw Short URL content script.
// MV3 content script 跑在「隔離世界」，page-interceptor.js 跑在「頁面主世界」。
// 兩個世界的 window 全域屬性不共用，所以不能用 window.__xxx 旗標互相偵測；
// 主要用 window.postMessage 橋接，並保留 CustomEvent 相容舊版封包。

const SHORTENER_API = 'https://u.kuies.tw/api/public/shorten';
const PREVIEW_STATUS_API = 'https://u.kuies.tw/api/public/preview-status/';
const PREVIEW_WAIT_MS = 2500;
const PREVIEW_POLL_MS = 250;
const DEFAULT_AUTO_CLEAN = true;
const READY_EVENT = 'kuies-tracking-cleaner-ready';
const READY_REQUEST_EVENT = 'kuies-tracking-cleaner-ready-request';
const TOGGLE_EVENT = 'kuies-tracking-cleaner-toggle';
const COPY_EVENT = 'kuies-tracking-copy';
const BRIDGE_MESSAGE = 'kuies-tracking-cleaner-bridge';
const READY_TIMEOUT_MS = 10000;

let autoCleanEnabled = DEFAULT_AUTO_CLEAN;
let interceptorReady = false;
let readyTimeout = null;
let readyRequestStartedAt = 0;
let lastCopyKey = '';
let lastCopyAt = 0;

function postBridge(action, detail = {}) {
  window.postMessage({ source: 'kuies-content-script', type: BRIDGE_MESSAGE, action, detail }, '*');
}

function flushToggle(enabled) {
  window.dispatchEvent(new CustomEvent(TOGGLE_EVENT, {
    detail: { enabled: Boolean(enabled) },
  }));
  postBridge('toggle', { enabled: Boolean(enabled) });
}

function markInterceptorReady() {
  interceptorReady = true;
  if (readyTimeout) {
    clearTimeout(readyTimeout);
    readyTimeout = null;
  }
  // ready 訊號代表 page world listener 已經掛好；下一個 frame 補送最新狀態。
  requestAnimationFrame(() => flushToggle(autoCleanEnabled));
}

function requestInterceptorReady() {
  if (interceptorReady) {
    flushToggle(autoCleanEnabled);
    return;
  }
  window.dispatchEvent(new CustomEvent(READY_REQUEST_EVENT));
  postBridge('ready-request');
  if (!readyTimeout) {
    readyRequestStartedAt = Date.now();
    readyTimeout = setTimeout(() => {
      readyTimeout = null;
      if (!interceptorReady) {
        // 不假設失敗即成功；保留未就緒狀態，等 popup「更新狀態」或 storage 變動再重送 ready request。
        console.warn('[kuies.tw Cleaner] page-interceptor 未回報 ready，暫停自動縮短同步');
      }
    }, READY_TIMEOUT_MS);
  }
}

function injectPageInterceptor() {
  // 先掛 ready listener，再注入 script，避免 page-interceptor 很快 dispatch ready 時漏接。
  window.addEventListener(READY_EVENT, markInterceptorReady);

  if (document.documentElement.dataset.kuiesTrackingCleanerInjected !== '1') {
    document.documentElement.dataset.kuiesTrackingCleanerInjected = '1';
    const script = document.createElement('script');
    script.src = chrome.runtime.getURL('page-interceptor.js');
    script.addEventListener('load', () => {
      script.remove();
      requestInterceptorReady();
    });
    script.addEventListener('error', () => {
      script.remove();
      console.warn('[kuies.tw Cleaner] page-interceptor 注入失敗');
    });
    (document.head || document.documentElement).appendChild(script);
  }

  // 不讀 window.__kuiesTrackingCleanerInstalled：它在 page world，不在 content script 隔離世界。
  // 改用 postMessage/事件 ping-pong，已安裝或稍後安裝完成都能回覆 ready。
  requestInterceptorReady();
}

function sendToggle(enabled) {
  autoCleanEnabled = Boolean(enabled);
  if (interceptorReady) {
    flushToggle(autoCleanEnabled);
  } else {
    requestInterceptorReady();
  }
}

function cleanTrackingUrl(raw) {
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    if (host === 'threads.com' || host === 'www.threads.com' || host === 'threads.net' || host === 'www.threads.net') {
      url.searchParams.delete('xmt');
      url.searchParams.delete('slof');
    }
    if (host === 'instagram.com' || host === 'www.instagram.com') {
      ['igsh', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'utm_id'].forEach((param) => url.searchParams.delete(param));
    }
    if (host === 'facebook.com' || host === 'www.facebook.com' || host === 'm.facebook.com') {
      ['fbclid', 'mibextid', 'rdid', 'share_url', '__cft__[0]', '__tn__', 'ref', 'refsrc'].forEach((param) => url.searchParams.delete(param));
    }
    return url.toString();
  } catch (error) {
    return raw;
  }
}

function previewCodeFromShortUrl(shortUrl) {
  try {
    const parsed = new URL(shortUrl);
    if (parsed.origin !== 'https://u.kuies.tw') return '';
    const code = decodeURIComponent(parsed.pathname.replace(/^\/+/, ''));
    return /^[A-Za-z0-9_-]{1,64}$/.test(code) ? code : '';
  } catch (error) {
    return '';
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForPreview(shortUrl, initialStatus = 'pending', options = {}) {
  const code = previewCodeFromShortUrl(shortUrl);
  if (!code) return shortUrl;
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : PREVIEW_WAIT_MS;
  const pollMs = Number.isFinite(options.pollMs) ? options.pollMs : PREVIEW_POLL_MS;
  const fetchImpl = options.fetchImpl || fetch;
  if (['ready', 'profile_fallback', 'fallback'].includes(initialStatus) && options.initialAvailable) {
    return shortUrl;
  }
  const deadline = Date.now() + Math.max(0, timeoutMs);
  while (Date.now() < deadline) {
    try {
      const response = await fetchImpl(`${PREVIEW_STATUS_API}${encodeURIComponent(code)}`, {
        method: 'GET',
        cache: 'no-store',
      });
      if (response.ok) {
        const state = await response.json();
        if (['ready', 'profile_fallback', 'fallback'].includes(state.preview_status) && state.preview_available) {
          return shortUrl;
        }
      }
    } catch (error) {
      // 暫時網路錯誤不降級長網址；在總等待上限內繼續短輪詢。
    }
    const remaining = deadline - Date.now();
    if (remaining > 0) await sleep(Math.min(pollMs, remaining));
  }
  return shortUrl;
}

async function shortenUrl(url) {
  const response = await fetch(SHORTENER_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url,
      clean_tracking: true,
      fast_response: true,
      title: '已移除追蹤參數的分享連結',
    }),
  });
  const data = await response.json();
  if (!response.ok || !data.short_url) {
    throw new Error(data.error || 'shorten_failed');
  }
  return waitForPreview(data.short_url, data.preview_status, {
    initialAvailable: Boolean(data.preview_available),
  });
}

async function replaceClipboardWithShortUrl(cleanedUrl) {
  if (!autoCleanEnabled) return;
  const shortUrl = await shortenUrl(cleanTrackingUrl(cleanedUrl));
  if (!autoCleanEnabled) return;
  await navigator.clipboard.writeText(shortUrl);
}

function handleCopyDetail(detail) {
  if (!autoCleanEnabled) return;
  const cleanedUrl = detail && detail.cleanedUrl;
  if (!cleanedUrl) return;
  const now = Date.now();
  const copyKey = `${cleanedUrl}|${detail.sourceText || ''}`;
  if (copyKey === lastCopyKey && now - lastCopyAt < 1500) return;
  lastCopyKey = copyKey;
  lastCopyAt = now;
  replaceClipboardWithShortUrl(cleanedUrl).catch(() => {
    // 若縮網址 API 暫時失敗，頁面攔截器已先把剪貼簿內容清掉追蹤參數。
  });
}

async function handleShortenRequest(detail) {
  const requestId = detail && detail.requestId;
  const cleanedUrl = detail && detail.cleanedUrl;
  if (!requestId || !cleanedUrl) return;
  if (!autoCleanEnabled) {
    postBridge('shorten-error', { requestId, error: 'auto_clean_disabled' });
    return;
  }
  try {
    const shortUrl = await shortenUrl(cleanTrackingUrl(cleanedUrl));
    postBridge('shorten-result', { requestId, shortUrl });
  } catch (error) {
    postBridge('shorten-error', {
      requestId,
      error: error && error.message ? error.message : 'shorten_failed',
    });
  }
}

window.addEventListener(COPY_EVENT, (event) => {
  handleCopyDetail(event.detail);
});

window.addEventListener('message', (event) => {
  if (event.source !== window) return;
  const message = event.data;
  if (!message || message.source !== 'kuies-page-interceptor' || message.type !== BRIDGE_MESSAGE) return;
  if (message.action === 'ready') {
    markInterceptorReady();
    return;
  }
  if (message.action === 'copy') {
    handleCopyDetail(message.detail);
    return;
  }
  if (message.action === 'shorten-request') {
    handleShortenRequest(message.detail);
  }
});

injectPageInterceptor();

chrome.storage.local.get({ autoCleanEnabled: DEFAULT_AUTO_CLEAN }, (items) => {
  sendToggle(items.autoCleanEnabled);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.autoCleanEnabled) return;
  sendToggle(changes.autoCleanEnabled.newValue);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'kuies-sync-auto-clean-state') return false;
  chrome.storage.local.get({ autoCleanEnabled: DEFAULT_AUTO_CLEAN }, (items) => {
    const enabled = Boolean(items.autoCleanEnabled);
    sendToggle(enabled);
    sendResponse({
      ok: true,
      autoCleanEnabled: enabled,
      interceptorReady,
      readyRequestAgeMs: readyRequestStartedAt ? Date.now() - readyRequestStartedAt : 0,
    });
  });
  return true;
});

window.__threadsLinkCleaner = { cleanTrackingUrl, shortenUrl, waitForPreview };
