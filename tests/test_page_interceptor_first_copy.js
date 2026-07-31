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
  class Clipboard {
    constructor() {
      this.writes = [];
    }

    async writeText(text) {
      await new Promise((resolve) => setTimeout(resolve, 15));
      this.writes.push(text);
    }
  }
  const clipboard = new Clipboard();

  function Navigator() {}
  Object.defineProperty(Navigator.prototype, 'clipboard', {
    get() {
      return clipboard;
    },
    configurable: true,
  });

  const documentTarget = new FakeEventTarget();
  documentTarget.execCommand = undefined;
  documentTarget.activeElement = null;

  Object.assign(windowTarget, {
    window: windowTarget,
    Navigator,
    Clipboard,
    navigator: new Navigator(),
    document: documentTarget,
    CustomEvent: FakeCustomEvent,
    getSelection() {
      return { toString: () => '' };
    },
  });

  const shortUrl = 'https://u.kuies.tw/T01';
  const bridgeMessages = [];
  windowTarget.postMessage = (data) => {
    bridgeMessages.push(data);
    windowTarget.dispatchEvent({ type: 'message', source: windowTarget, data });
    if (data && data.source === 'kuies-page-interceptor' && data.action === 'shorten-request') {
      setTimeout(() => {
        windowTarget.postMessage({
          source: 'kuies-content-script',
          type: data.type,
          action: 'shorten-result',
          detail: { requestId: data.detail.requestId, shortUrl },
        });
      }, 5);
    }
  };

  const context = vm.createContext({
    window: windowTarget,
    document: documentTarget,
    navigator: windowTarget.navigator,
    Navigator,
    Clipboard,
    CustomEvent: FakeCustomEvent,
    URL,
    Blob,
    Proxy,
    Object,
    Array,
    Map,
    Promise,
    setTimeout,
    clearTimeout,
    console,
  });

  const interceptorPath = path.join(__dirname, '..', 'extension', 'threads-link-cleaner', 'page-interceptor.js');
  vm.runInContext(fs.readFileSync(interceptorPath, 'utf8'), context, { filename: interceptorPath });

  windowTarget.dispatchEvent(new FakeCustomEvent('kuies-tracking-cleaner-toggle', {
    detail: { enabled: true },
  }));

  const trackedUrl = 'https://www.threads.com/@abc/post/first?xmt=tracking';
  await windowTarget.navigator.clipboard.writeText(trackedUrl);
  await new Promise((resolve) => setTimeout(resolve, 30));

  const request = bridgeMessages.find((message) => message && message.action === 'shorten-request');
  assert(request, '第一次 writeText 必須等待 content script 回傳短網址，而不是先寫入清理後原始網址');
  assert(clipboard.writes.length === 1, `剪貼簿應只寫入一次，實際為 ${clipboard.writes.length} 次`);
  assert(clipboard.writes[0] === shortUrl, `剪貼簿應寫入短網址，實際為 ${clipboard.writes[0]}`);

  const facebookShareUrl = 'https://www.facebook.com/share/p/1bCsBTnWp7/';
  await windowTarget.navigator.clipboard.writeText(facebookShareUrl);
  await new Promise((resolve) => setTimeout(resolve, 30));
  const requests = bridgeMessages.filter((message) => message && message.action === 'shorten-request');
  assert(requests.length === 2, 'Facebook /share/p/ 連結也必須送交短網址 API 解析');
  assert(requests[1].detail.cleanedUrl === facebookShareUrl, 'Facebook 包裝網址應完整送交後端解析');
  assert(clipboard.writes.length === 2, 'Facebook 複製流程也只能新增一次最終剪貼簿寫入');
  assert(clipboard.writes[1] === shortUrl, 'Facebook 複製流程應寫入自己的短網址');

  process.stdout.write('first-copy-short-url-and-facebook-share: ok\n');
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
