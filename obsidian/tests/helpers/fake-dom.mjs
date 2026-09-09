// Minimal DOM stub implementing the subset of Obsidian/DOM element APIs used by
// src/console/console-dom.js and src/console/register.js, so render logic can be
// tested under plain Node without Obsidian.
export class FakeStyle {
  constructor() {
    this.props = {};
  }
  setProperty(name, value) {
    this.props[name] = String(value);
  }
  getPropertyValue(name) {
    return Object.prototype.hasOwnProperty.call(this.props, name) ? this.props[name] : null;
  }
}

export class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.classes = new Set();
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.value = '';
    this.parent = null;
    this.style = new FakeStyle();
  }

  get className() {
    return [...this.classes].join(' ');
  }

  set className(value) {
    this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  addClass(cls) {
    for (const c of String(cls).split(/\s+/).filter(Boolean)) this.classes.add(c);
  }

  removeClass(cls) {
    this.classes.delete(cls);
  }

  toggleClass(cls, force) {
    if (force) this.classes.add(cls);
    else this.classes.delete(cls);
  }

  setText(text) {
    this.children = [];
    this.textContent = String(text);
  }

  empty() {
    this.children = [];
    this.textContent = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  addEventListener(event, fn) {
    (this.listeners[event] ||= []).push(fn);
  }

  removeEventListener(event, fn) {
    const list = this.listeners[event];
    if (list) {
      const index = list.indexOf(fn);
      if (index >= 0) list.splice(index, 1);
    }
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  // 焦点追踪：document.activeElement 语义，供抽屉焦点圈/焦点归还断言。
  focus() {
    const doc = this.ownerDocument;
    if (doc) doc.activeElement = this;
    this.focused = true;
  }

  // 最小事件派发：只走 addEventListener 注册的监听器，供 'input'/'keydown'
  // 这类需要穿过既有 handler 的测试使用。原生 Event 的 target 只读，赋值
  // 失败时容忍（真实 DOM 由浏览器在派发时设置 target）。
  dispatchEvent(event) {
    try {
      if (event && event.target == null) event.target = this;
    } catch { /* native Event target is read-only */ }
    for (const fn of this.listeners[event.type] || []) fn(event);
    return true;
  }

  get ownerDocument() {
    let node = this;
    while (node.parent) node = node.parent;
    return node.tagName === 'DOCUMENT' ? node : null;
  }

  // Mirrors DOM Node.isConnected: true when the ancestor chain reaches the
  // document node. Removing a subtree (Dataview re-render wiping the old
  // homepage root) therefore flips isConnected to false, which is what the
  // Cosmos mount handshake uses to discard late async results.
  get isConnected() {
    return this.ownerDocument !== null;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (typeof node.matches === 'function' && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  insertBefore(el, ref) {
    if (el.parent) el.remove();
    el.parent = this;
    const index = ref ? this.children.indexOf(ref) : -1;
    if (index >= 0) this.children.splice(index, 0, el);
    else this.children.push(el);
    return el;
  }

  click() {
    // 对齐真实 DOM：disabled button 不派发 click（浏览器不触发 listener，
    // 也不做激活）。此前 FakeEl 无视 disabled 派发，导致测试通过而真实
    // 行为不同（或反之）的保真度缺口。
    if (this.tagName === 'BUTTON' && this.getAttribute('disabled') !== null) return false;
    for (const fn of this.listeners.click || []) fn({ target: this });
    return true;
  }

  appendChild(el) {
    el.parent = this;
    this.children.push(el);
    return el;
  }

  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
      this.parent = null;
    }
  }

  createElement(tag) {
    return new FakeEl(tag);
  }

  createEl(tag, opts) {
    const el = new FakeEl(tag);
    if (opts && opts.cls) el.addClass(opts.cls);
    if (opts && typeof opts.text !== 'undefined') el.setText(opts.text);
    this.appendChild(el);
    return el;
  }

  createDiv(opts) {
    return this.createEl('div', opts);
  }

  createSpan(opts) {
    return this.createEl('span', opts);
  }

  matches(selector) {
    const parts = selector.trim().split(/\s+/);
    const last = parts[parts.length - 1];
    if (!matchSimple(this, last)) return false;
    let node = this.parent;
    for (let i = parts.length - 2; i >= 0; i -= 1) {
      const part = parts[i];
      if (part === '>') {
        // 直接子组合器：下一 part 必须匹配当前父节点（不冒泡）
        i -= 1;
        if (i < 0 || !node || !matchSimple(node, parts[i])) return false;
        node = node.parent;
        continue;
      }
      while (node && !matchSimple(node, part)) node = node.parent;
      if (!node) return false;
      node = node.parent;
    }
    return true;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (child.matches(selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  get text() {
    return this.textContent + this.children.map((c) => c.text).join('');
  }

  focusables() {
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (child.tagName === 'BUTTON' || child.tagName === 'SELECT' || child.tagName === 'A' || child.tagName === 'INPUT') out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

function matchSimple(el, simple) {
  let rest = simple;
  let attr = null;
  const attrStart = rest.indexOf('[');
  if (attrStart >= 0) {
    attr = rest.slice(attrStart + 1, rest.indexOf(']'));
    rest = rest.slice(0, attrStart);
  }
  if (rest.startsWith('.')) {
    if (!el.classes.has(rest.slice(1))) return false;
  } else if (rest && el.tagName !== rest.toUpperCase()) {
    return false;
  }
  if (attr && el.getAttribute(attr) === null) return false;
  return true;
}
