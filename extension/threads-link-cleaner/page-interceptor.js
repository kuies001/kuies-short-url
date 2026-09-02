// 注入到頁面「主世界」的攔截器。
// 注意：這個 IIFE 內的 listener、Proxy 都掛在「頁面的 window」上，
// 而 content script 是在隔離世界，兩邊主要用 window.postMessage 溝通。
// 因此這裡的程式碼不可以依賴 content script 的變數。

(() => {
  'use strict';

  const BRIDGE_MESSAGE = 'kuies-tracking-cleaner-bridge';
  const SHORTEN_TIMEOUT_MS = 2500;
  const pendingShortenRequests = new Map();
  let shortenRequestSequence = 0;

  function postBridge(action, detail = {}) {
    window.postMessage({ source: 'kuies-page-interceptor', type: BRIDGE_MESSAGE, action, detail }, '*');
  }

  function dispatchReady() {
    window.dispatchEvent(new CustomEvent('kuies-tracking-cleaner-ready', {
      detail: { ready: true, version: '1.7.4' },
    }));
    postBridge('ready', { ready: true, version: '1.7.4' });
  }

  if (window.__kuiesTrackingCleanerInstalled) {
    dispatchReady();
    return;
  }

  let autoCleanEnabled = false;
  window.__kuiesTrackingCleanerAutoCleanEnabled = false;

  window.addEventListener('kuies-tracking-cleaner-ready-request', dispatchReady);

  window.addEventListener('kuies-tracking-cleaner-toggle', (event) => {
    autoCleanEnabled = Boolean(event && event.detail && event.detail.enabled);
    window.__kuiesTrackingCleanerAutoCleanEnabled = autoCleanEnabled;
  });

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const message = event.data;
    if (!message || message.source !== 'kuies-content-script' || message.type !== BRIDGE_MESSAGE) return;
    if (message.action === 'ready-request') {
      dispatchReady();
      return;
    }
    if (message.action === 'toggle') {
      autoCleanEnabled = Boolean(message.detail && message.detail.enabled);
      window.__kuiesTrackingCleanerAutoCleanEnabled = autoCleanEnabled;
      return;
    }
    if (message.action === 'shorten-result' || message.action === 'shorten-error') {
      const detail = message.detail || {};
      const pending = pendingShortenRequests.get(detail.requestId);
      if (!pending) return;
      pendingShortenRequests.delete(detail.requestId);
      clearTimeout(pending.timeout);
      if (message.action === 'shorten-result' && detail.shortUrl) {
        pending.resolve(detail.shortUrl);
      } else {
        pending.reject(new Error(detail.error || 'shorten_failed'));
      }
    }
  });

  const TRACKING_PARAMS = {
    'threads.': ['xmt', 'slof'],
    'instagram.': ['igsh', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'utm_id'],
    'facebook.': ['fbclid', 'mibextid', 'rdid', 'share_url', '__cft__[0]', '__tn__', 'ref', 'refsrc'],
  };

  const TRACKING_URL_RE = /https?:\/\/(?:(?:www\.)?threads\.(?:com|net)|(?:www\.)?instagram\.com|(?:(?:www|m)\.)?facebook\.com)\/[^\s"'<>]+/g;

  function cleanUrl(text) {
    if (!text) return text;
    try {
      const url = new URL(text);
      let changed = false;
      for (const [domain, params] of Object.entries(TRACKING_PARAMS)) {
        if (url.hostname.includes(domain)) {
          params.forEach((param) => {
            if (url.searchParams.has(param)) {
              url.searchParams.delete(param);
              changed = true;
            }
          });
          break;
        }
      }
      return changed ? url.toString() : text;
    } catch (error) {
      return text;
    }
  }

  function cleanText(text) {
    if (!text) return text;
    return text.replace(TRACKING_URL_RE, cleanUrl);
  }

  function findFirstTrackingUrl(text) {
    const match = text && text.match(TRACKING_URL_RE);
    return match ? match[0] : '';
  }

  function notifyShorten(sourceText) {
    if (!autoCleanEnabled) return;
    const url = findFirstTrackingUrl(sourceText);
    if (!url) return;
    const cleanedUrl = cleanUrl(url);
    window.dispatchEvent(new CustomEvent('kuies-tracking-copy', {
      detail: { url, cleanedUrl, sourceText },
    }));
    postBridge('copy', { url, cleanedUrl, sourceText });
  }

  function requestShortUrl(sourceText) {
    const url = findFirstTrackingUrl(sourceText);
    if (!url) return Promise.resolve(sourceText);
    const cleanedUrl = cleanUrl(url);
    const requestId = `copy-${Date.now()}-${++shortenRequestSequence}`;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pendingShortenRequests.delete(requestId);
        reject(new Error('shorten_timeout'));
      }, SHORTEN_TIMEOUT_MS);
      pendingShortenRequests.set(requestId, { resolve, reject, timeout });
      postBridge('shorten-request', { requestId, url, cleanedUrl, sourceText });
    });
  }

  async function shortenBeforeClipboardWrite(sourceText) {
    const cleaned = cleanText(sourceText);
    if (!autoCleanEnabled || !findFirstTrackingUrl(cleaned)) return cleaned;
    try {
      return await requestShortUrl(cleaned);
    } catch (error) {
      console.warn('[kuies.tw Cleaner] 首次縮短失敗，改寫入已清理網址:', error);
      return cleaned;
    }
  }

  // 僅包裝 Clipboard 的寫入方法，不改寫 Navigator.prototype.clipboard。
  // 原本每次讀取 navigator.clipboard 都建立新 Proxy，破壞瀏覽器的 [SameObject]
  // 物件身分保證，可能讓 Threads 圖片檢視器等原生 UI 的事件狀態失效。
  const clipboardProto = window.Clipboard && window.Clipboard.prototype;
  const writeTextDescriptor = clipboardProto && Object.getOwnPropertyDescriptor(clipboardProto, 'writeText');
  if (writeTextDescriptor && typeof writeTextDescriptor.value === 'function') {
    const originalWriteText = writeTextDescriptor.value;
    Object.defineProperty(clipboardProto, 'writeText', {
      ...writeTextDescriptor,
      value: async function writeText(text) {
        if (!autoCleanEnabled) return originalWriteText.call(this, text);
        // 僅執行一次最終剪貼簿寫入。先等待縮網址完成，失敗才回退至已清理網址。
        const finalText = await shortenBeforeClipboardWrite(text);
        return originalWriteText.call(this, finalText);
      },
    });
  }

  const writeDescriptor = clipboardProto && Object.getOwnPropertyDescriptor(clipboardProto, 'write');
  if (writeDescriptor && typeof writeDescriptor.value === 'function') {
    const originalWrite = writeDescriptor.value;
    Object.defineProperty(clipboardProto, 'write', {
      ...writeDescriptor,
      value: async function write(items) {
        if (!autoCleanEnabled) return originalWrite.call(this, items);
        try {
          const cleanedItems = await Promise.all(items.map(async (item) => {
            if (!(item instanceof window.ClipboardItem)) return item;
            const types = item.types;
            if (!types.includes('text/plain')) return item;
            const blob = await item.getType('text/plain');
            const text = await blob.text();
            const finalText = await shortenBeforeClipboardWrite(text);
            if (finalText === text) return item;
            const newData = {};
            for (const type of types) {
              newData[type] = type === 'text/plain'
                ? new window.Blob([finalText], { type: 'text/plain' })
                : await item.getType(type);
            }
            return new window.ClipboardItem(newData);
          }));
          return originalWrite.call(this, cleanedItems);
        } catch (error) {
          return originalWrite.call(this, items);
        }
      },
    });
  }

  const originalExecCommand = window.document.execCommand && window.document.execCommand.bind(window.document);
  if (originalExecCommand) {
    window.document.execCommand = function execCommand(command, ...args) {
      if (autoCleanEnabled && String(command).toLowerCase() === 'copy') {
        try {
          const selection = window.getSelection();
          const selectedText = selection ? selection.toString() : '';
          const cleaned = cleanText(selectedText);
          if (cleaned !== selectedText && cleaned) {
            const activeEl = window.document.activeElement;
            if (activeEl && (activeEl.tagName === 'TEXTAREA' || activeEl.tagName === 'INPUT')) {
              const original = activeEl.value;
              activeEl.value = cleaned;
              activeEl.select();
              const result = originalExecCommand(command, ...args);
              activeEl.value = original;
              notifyShorten(cleaned);
              return result;
            }
            notifyShorten(cleaned);
          }
        } catch (error) {
          console.warn('[kuies.tw Cleaner] execCommand 攔截失敗:', error);
        }
      }
      return originalExecCommand(command, ...args);
    };
  }

  document.addEventListener('copy', (event) => {
    if (!autoCleanEnabled) return;
    // 只處理瀏覽器實際由使用者觸發、且提供可寫剪貼簿資料的 copy。
    // Threads 內部合成事件與其他 UI 事件不得被 preventDefault。
    if (!event.isTrusted || !event.clipboardData || typeof event.clipboardData.setData !== 'function') return;
    const selection = window.getSelection ? window.getSelection().toString() : '';
    const current = typeof event.clipboardData.getData === 'function'
      ? event.clipboardData.getData('text/plain')
      : '';
    const source = current || selection;
    if (!source || !TRACKING_URL_RE.test(source)) {
      TRACKING_URL_RE.lastIndex = 0;
      return;
    }
    TRACKING_URL_RE.lastIndex = 0;
    const cleaned = cleanText(source);
    if (!cleaned) return;
    if (cleaned !== source) {
      event.preventDefault();
      event.clipboardData.setData('text/plain', cleaned);
      event.clipboardData.setData('text/html', cleaned);
    }
    // Facebook `/share/.../` 可能沒有 query 可刪，cleaned 會等於 source；
    // 仍須送交後端解析包裝網址並縮短，不能因字串未變而提前結束。
    notifyShorten(cleaned);
  }, true);

  // 旗標保留給 page world 除錯使用；content script 不能讀這個旗標，
  // 跨隔離世界同步優先靠 postMessage；CustomEvent 保留相容。
  window.__kuiesTrackingCleaner = { cleanUrl, cleanText, findFirstTrackingUrl, getAutoCleanEnabled: () => autoCleanEnabled };
  window.__kuiesTrackingCleanerInstalled = true;
  dispatchReady();
  console.info('[kuies.tw Cleaner] 已就緒：Threads/Facebook/IG 自動清理與縮短');
})();
