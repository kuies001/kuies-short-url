const SHORTENER_API = 'https://u.kuies.tw/api/public/shorten';
const URL_RE = /https?:\/\/[^\s"'<>]+/g;
const DEFAULT_AUTO_CLEAN = true;

function findFirstUrl(text) {
  const match = text.match(URL_RE);
  return match ? match[0] : '';
}

async function getCurrentTabUrl() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tabs && tabs[0] && tabs[0].url ? tabs[0].url : '';
  if (!/^https?:\/\//.test(url)) {
    throw new Error('目前分頁不是可縮短的 http 或 https 網址。');
  }
  return url;
}

async function sendStateToCurrentTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tabs && tabs[0] && tabs[0].id;
  if (!tabId) return false;
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: 'kuies-sync-auto-clean-state' });
    return Boolean(response && response.ok);
  } catch (error) {
    return false;
  }
}

async function shortenUrl(url, options = {}) {
  const response = await fetch(SHORTENER_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url,
      clean_tracking: Boolean(options.cleanTracking),
      title: options.cleanTracking ? '已移除追蹤參數的分享連結' : '目前分頁連結',
    }),
  });
  const data = await response.json();
  if (!response.ok || !data.short_url) {
    throw new Error(data.error || 'shorten_failed');
  }
  return data;
}

function setStatus(message, isHtml = false) {
  const statusEl = document.getElementById('status');
  if (isHtml) {
    statusEl.innerHTML = message;
  } else {
    statusEl.textContent = message;
  }
}

function escapeHtml(text) {
  return text.replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[char]));
}

async function writeResultToClipboard(data) {
  await navigator.clipboard.writeText(data.short_url);
  const safeShortUrl = escapeHtml(data.short_url);
  const safeTargetUrl = escapeHtml(data.target_url);
  setStatus(
    `已建立並寫回剪貼簿：<br><a href="${safeShortUrl}" target="_blank" rel="noopener">${safeShortUrl}</a><br><span class="target">目標：${safeTargetUrl}</span>`,
    true,
  );
}

async function shortenCurrentTab() {
  const url = await getCurrentTabUrl();
  setStatus('正在縮短目前分頁網址…');
  const data = await shortenUrl(url, { cleanTracking: false });
  await writeResultToClipboard(data);
}

async function shortenClipboardWithTrackingClean() {
  const text = await navigator.clipboard.readText();
  const url = findFirstUrl(text);
  if (!url) {
    setStatus('剪貼簿沒有可縮短的網址。');
    return;
  }

  setStatus('正在移除追蹤參數並縮短剪貼簿網址…');
  const data = await shortenUrl(url, { cleanTracking: true });
  await writeResultToClipboard(data);
}

async function loadAutoCleanToggle() {
  const toggle = document.getElementById('autoCleanToggle');
  const { autoCleanEnabled } = await chrome.storage.local.get({ autoCleanEnabled: DEFAULT_AUTO_CLEAN });
  toggle.checked = Boolean(autoCleanEnabled);
  toggle.addEventListener('change', async () => {
    await chrome.storage.local.set({ autoCleanEnabled: toggle.checked });
    const synced = await sendStateToCurrentTab();
    setStatus(toggle.checked
      ? `已開啟自動去除追蹤並縮短。${synced ? '' : '目前分頁尚未同步，請重整該頁或按更新狀態。'}`
      : `已關閉自動模式，保留手動縮短功能。${synced ? '' : '目前分頁尚未同步，請重整該頁或按更新狀態。'}`);
  });
}

document.getElementById('syncState').addEventListener('click', async () => {
  const synced = await sendStateToCurrentTab();
  const { autoCleanEnabled } = await chrome.storage.local.get({ autoCleanEnabled: DEFAULT_AUTO_CLEAN });
  document.getElementById('autoCleanToggle').checked = Boolean(autoCleanEnabled);
  setStatus(synced
    ? `已同步目前分頁狀態：${autoCleanEnabled ? '自動模式開啟' : '手動模式'}`
    : '目前分頁不是 Threads / Facebook / IG，或需要重新整理頁面後再同步。');
});

document.getElementById('shorten').addEventListener('click', async () => {
  try {
    await shortenCurrentTab();
  } catch (error) {
    setStatus(error.message || '縮短目前分頁網址失敗，請稍後再試，或直接使用 https://u.kuies.tw/surl。');
  }
});

document.getElementById('shortenClean').addEventListener('click', async () => {
  try {
    await shortenClipboardWithTrackingClean();
  } catch (error) {
    setStatus(error.message || '移除追蹤參數或縮短剪貼簿網址失敗，請稍後再試，或直接使用 https://u.kuies.tw/surl。');
  }
});

loadAutoCleanToggle().catch(() => {
  setStatus('讀取自動模式設定失敗，但手動功能仍可使用。');
});
