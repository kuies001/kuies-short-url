const fs = require('fs');
const vm = require('vm');
const path = require('path');

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    const listeners = this.listeners.get(event.type) || [];
    for (const listener of listeners) listener.call(this, event);
    return true;
  }
}

class FakeCustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
  }
}

async function main() {
  const windowTarget = new FakeEventTarget();
  const documentTarget = new FakeEventTarget();
  documentTarget.documentElement = { dataset: {} };
  documentTarget.head = { appendChild() {} };
  documentTarget.createElement = () => ({ addEventListener() {}, remove() {} });
  documentTarget.execCommand = undefined;
  documentTarget.activeElement = null;

  class Clipboard {
    constructor() {
      this.writes = [];
    }

    async writeText(text) {
      this.writes.push(text);
    }
  }
  const clipboard = new Clipboard();
  function Navigator() {}
  Object.defineProperty(Navigator.prototype, 'clipboard', {
    get() { return clipboard; },
    configurable: true,
  });

  const shortUrl = 'https://u.kuies.tw/R25';
  let shortenPosts = 0;
  let previewGets = 0;
  const fetch = async (url, options = {}) => {
    if (url === 'https://u.kuies.tw/api/public/shorten' && options.method === 'POST') {
      shortenPosts += 1;
      if (shortenPosts === 2) {
        return {
          ok: false,
          async json() { return { error: 'rate_limited' }; },
        };
      }
      return {
        ok: true,
        async json() {
          return { short_url: shortUrl, preview_status: 'pending', preview_available: false };
        },
      };
    }
    if (String(url).includes('/api/public/preview-status/')) {
      previewGets += 1;
      return {
        ok: true,
        async json() { return { preview_status: 'pending', preview_available: false }; },
      };
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  Object.assign(windowTarget, {
    window: windowTarget,
    document: documentTarget,
    Navigator,
    Clipboard,
    navigator: new Navigator(),
    CustomEvent: FakeCustomEvent,
    getSelection() { return { toString: () => '' }; },
  });
  windowTarget.postMessage = (data) => {
    windowTarget.dispatchEvent({ type: 'message', source: windowTarget, data });
  };

  const chrome = {
    runtime: { getURL: (name) => name, onMessage: { addListener() {} } },
    storage: {
      local: { get(defaults, callback) { callback(defaults); } },
      onChanged: { addListener() {} },
    },
  };
  const context = vm.createContext({
    window: windowTarget,
    document: documentTarget,
    navigator: windowTarget.navigator,
    Navigator,
    Clipboard,
    CustomEvent: FakeCustomEvent,
    chrome,
    fetch,
    URL,
    Blob,
    Proxy,
    Object,
    Array,
    Map,
    Promise,
    requestAnimationFrame: (callback) => callback(),
    setTimeout,
    clearTimeout,
    console,
    Date,
  });

  const extensionDir = path.join(__dirname, '..', 'extension', 'threads-link-cleaner');
  vm.runInContext(fs.readFileSync(path.join(extensionDir, 'content.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(extensionDir, 'page-interceptor.js'), 'utf8'), context);
  windowTarget.dispatchEvent(new FakeCustomEvent('kuies-tracking-cleaner-toggle', {
    detail: { enabled: true },
  }));

  const trackedUrl = 'https://www.threads.com/@abc/post/race?xmt=tracking';
  const startedAt = Date.now();
  await windowTarget.navigator.clipboard.writeText(trackedUrl);
  const elapsedMs = Date.now() - startedAt;

  assert(shortenPosts === 1, `首次複製應只送出一次 shorten POST，實際 ${shortenPosts} 次`);
  assert(clipboard.writes.length === 1, `剪貼簿應只寫入一次，實際 ${clipboard.writes.length} 次`);
  assert(clipboard.writes[0] === shortUrl, `第一次剪貼簿內容應為短網址，實際 ${clipboard.writes[0]}`);
  assert(elapsedMs < 1000, `首次複製只應等待 shorten POST，不得等待 2500ms 預覽，實際 ${elapsedMs}ms`);
  assert(previewGets >= 0, '預覽暖機可於背景執行，但不得阻塞複製');

  const failedTrackedUrl = 'https://www.threads.com/@abc/post/fallback?xmt=tracking&slof=extra';
  await windowTarget.navigator.clipboard.writeText(failedTrackedUrl);
  assert(shortenPosts === 2, `API 失敗案例應再送一次 shorten POST，實際共 ${shortenPosts} 次`);
  assert(clipboard.writes.length === 2, `API 失敗也只能新增一次剪貼簿寫入，實際共 ${clipboard.writes.length} 次`);
  assert(
    clipboard.writes[1] === 'https://www.threads.com/@abc/post/fallback',
    `只有公開 API 失敗時才應降級為已清理長網址，實際 ${clipboard.writes[1]}`,
  );

  process.stdout.write(`first-copy-timeout-race-and-api-fallback: ok (${elapsedMs}ms)\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
