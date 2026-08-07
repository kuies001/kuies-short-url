const fs = require('fs');
const vm = require('vm');
const path = require('path');

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const listeners = new Map();
  const windowObject = {
    addEventListener(type, handler) { listeners.set(type, handler); },
    dispatchEvent() { return true; },
    postMessage() {},
  };
  windowObject.window = windowObject;
  const documentElement = { dataset: {} };
  const documentObject = {
    documentElement,
    head: { appendChild() {} },
    createElement() {
      return {
        addEventListener() {},
        remove() {},
        set src(value) { this._src = value; },
      };
    },
  };
  const chrome = {
    runtime: { getURL: (name) => name, onMessage: { addListener() {} } },
    storage: {
      local: { get(defaults, callback) { callback(defaults); } },
      onChanged: { addListener() {} },
    },
  };
  let statusCalls = 0;
  const fetch = async (url) => {
    if (String(url).includes('/preview-status/')) {
      statusCalls += 1;
      const ready = statusCalls >= 3;
      return {
        ok: true,
        async json() {
          return {
            preview_status: ready ? 'ready' : 'pending',
            preview_available: ready,
          };
        },
      };
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const testSetTimeout = (callback, ms) => {
    const timer = setTimeout(callback, ms);
    if (ms > 1000 && timer.unref) timer.unref();
    return timer;
  };
  const context = vm.createContext({
    window: windowObject,
    document: documentObject,
    navigator: { clipboard: { async writeText() {} } },
    chrome,
    fetch,
    URL,
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    requestAnimationFrame: (callback) => callback(),
    setTimeout: testSetTimeout,
    clearTimeout,
    console,
    Date,
    Promise,
  });
  const contentPath = path.join(__dirname, '..', 'extension', 'threads-link-cleaner', 'content.js');
  vm.runInContext(fs.readFileSync(contentPath, 'utf8'), context, { filename: contentPath });
  const api = windowObject.__threadsLinkCleaner;
  assert(api && typeof api.waitForPreview === 'function', 'content script 必須公開可測試的 waitForPreview');

  const shortUrl = 'https://u.kuies.tw/Ab3';
  const readyResult = await api.waitForPreview(shortUrl, 'pending', { timeoutMs: 100, pollMs: 5 });
  assert(readyResult === shortUrl, '預覽就緒後仍必須回傳短網址');
  assert(statusCalls === 3, `應輪詢至 ready，實際 ${statusCalls} 次`);

  statusCalls = 0;
  const neverReadyFetch = async () => ({
    ok: true,
    async json() { return { preview_status: 'pending', preview_available: false }; },
  });
  const timeoutResult = await api.waitForPreview(shortUrl, 'pending', {
    timeoutMs: 25,
    pollMs: 5,
    fetchImpl: neverReadyFetch,
  });
  assert(timeoutResult === shortUrl, '逾時不得降級回長網址');
  assert(timeoutResult !== 'https://www.threads.com/@a/post/1', '逾時結果不可是長網址');

  process.stdout.write('preview-wait-and-short-url-timeout: ok\n');
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
