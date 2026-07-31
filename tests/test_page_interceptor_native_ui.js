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
    return !event.defaultPrevented;
  }
}

class FakeCustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
    this.defaultPrevented = false;
  }

  preventDefault() {
    this.defaultPrevented = true;
  }
}

async function main() {
  const windowTarget = new FakeEventTarget();
  const documentTarget = new FakeEventTarget();
  documentTarget.execCommand = undefined;
  documentTarget.activeElement = null;

  class Clipboard {
    async writeText() {}
  }
  const clipboard = new Clipboard();

  function Navigator() {}
  Object.defineProperty(Navigator.prototype, 'clipboard', {
    get() {
      return clipboard;
    },
    configurable: true,
  });

  Object.assign(windowTarget, {
    window: windowTarget,
    Navigator,
    Clipboard,
    navigator: new Navigator(),
    document: documentTarget,
    CustomEvent: FakeCustomEvent,
    getSelection() {
      return { toString: () => 'https://www.threads.com/@abc/post/one?xmt=tracking' };
    },
  });
  windowTarget.postMessage = () => {};

  const nativeClipboard = windowTarget.navigator.clipboard;
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

  assert(
    windowTarget.navigator.clipboard === nativeClipboard,
    '攔截器不得替換 navigator.clipboard 物件身分，否則可能破壞 Threads 原生 UI 的狀態判斷',
  );

  let viewerOpen = true;
  documentTarget.addEventListener('click', (event) => {
    if (event.target && event.target.role === 'close') viewerOpen = false;
  });
  const closeClick = {
    type: 'click',
    target: { role: 'close' },
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  documentTarget.dispatchEvent(closeClick);
  assert(!closeClick.defaultPrevented, 'Threads 原生關閉 click 不得被攔截器取消');
  assert(!viewerOpen, 'Threads 圖片檢視器的原生關閉處理器必須收到 click');

  const syntheticCopy = {
    type: 'copy',
    isTrusted: false,
    clipboardData: {
      getData: () => '',
      setData() {},
    },
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  documentTarget.dispatchEvent(syntheticCopy);
  assert(
    !syntheticCopy.defaultPrevented,
    '非使用者觸發的 copy 事件不是實際複製行為，不得 preventDefault',
  );

  const copied = {};
  const trustedCopy = {
    type: 'copy',
    isTrusted: true,
    clipboardData: {
      getData: () => '',
      setData(type, value) { copied[type] = value; },
    },
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  documentTarget.dispatchEvent(trustedCopy);
  assert(trustedCopy.defaultPrevented, '使用者實際複製含追蹤參數的網址時必須攔截');
  assert(
    copied['text/plain'] === 'https://www.threads.com/@abc/post/one',
    `實際複製應移除追蹤參數，結果為 ${copied['text/plain']}`,
  );

  process.stdout.write('native-close-click-and-real-copy-only: ok\n');
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
