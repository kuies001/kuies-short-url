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

const windowTarget = new FakeEventTarget();
const documentTarget = new FakeEventTarget();
const postImage = {
  currentSrc: 'https://scontent.cdninstagram.com/v/t51.71878-15/post.jpg?signature=temporary',
  src: 'https://scontent.cdninstagram.com/v/t51.71878-15/post.jpg?signature=temporary',
  alt: '',
  naturalWidth: 640,
  naturalHeight: 358,
};
const avatar = {
  currentSrc: 'https://scontent.cdninstagram.com/v/t51.2885-19/avatar.jpg',
  src: 'https://scontent.cdninstagram.com/v/t51.2885-19/avatar.jpg',
  alt: 'example.account的大頭貼照',
  naturalWidth: 36,
  naturalHeight: 36,
};
const postContainer = {
  innerText: 'example.account\n3小時\n『這是範例貼文的內容\n用來驗證預覽擷取』\n🫣🫣\n1\n/\n2\n370\n18\n19\n181',
  querySelectorAll(selector) {
    return selector === 'img' ? [avatar, postImage] : [];
  },
};
const postAnchor = {
  href: 'https://www.threads.com/@example.account/post/AbCdEf12345',
  closest(selector) {
    return selector === '[data-pressable-container="true"]' ? postContainer : null;
  },
};
documentTarget.querySelectorAll = (selector) => selector === 'a[href]' ? [postAnchor] : [];
documentTarget.execCommand = undefined;
documentTarget.activeElement = null;

Object.assign(windowTarget, {
  window: windowTarget,
  document: documentTarget,
  CustomEvent: FakeCustomEvent,
  getSelection() { return { toString: () => '' }; },
});
windowTarget.postMessage = (data) => {
  windowTarget.dispatchEvent({ type: 'message', source: windowTarget, data });
};

const context = vm.createContext({
  window: windowTarget,
  document: documentTarget,
  CustomEvent: FakeCustomEvent,
  URL,
  Map,
  Promise,
  setTimeout,
  clearTimeout,
  console,
});
const interceptorPath = path.join(__dirname, '..', 'extension', 'threads-link-cleaner', 'page-interceptor.js');
vm.runInContext(fs.readFileSync(interceptorPath, 'utf8'), context, { filename: interceptorPath });

const preview = windowTarget.__kuiesTrackingCleaner.collectPagePreview(postAnchor.href);
assert(preview, '應回傳目前 Threads 貼文的頁面預覽');
assert(preview.title.includes('這是範例貼文'), `標題應來自貼文文字，實際為 ${preview.title}`);
assert(preview.description.includes('用來驗證預覽擷取'), '描述應包含貼文正文');
assert(preview.image === postImage.currentSrc, '應選貼文圖片而不是個人頭像');

process.stdout.write('page-preview-metadata: ok\n');
