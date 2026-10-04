// tinycode app runner: loads a web page in a small simulated browser (Node's vm
// + a fake DOM built from the real HTML), runs its scripts, clicks every
// clickable element, presses keys, runs optional scenario steps and reports
// runtime errors, broken wiring and suspicious output as JSON.
//
// Design rule: a false alarm is worse than a miss. Anything the simulation
// doesn't model returns a harmless "universal stub" instead of undefined, so
// apps don't crash on missing browser APIs; only faithful lookups
// (getElementById / supported selectors) return null like a real browser.
'use strict';
const vm = require('vm');
const fs = require('fs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = { errors: [], warnings: [], trace: [], expects: [], probes: [], ran: [], skipped: [] };
const PAGE_FILES = new Set(input.files || []);
const MAX_CLICKS = input.max_clicks || 80;
const T0 = Date.now();
const BUDGET_MS = input.budget_ms || 6000;
let phase = 'loading';
let nullLookups = [];

// ------------------------------------------------------------- universal stub
const STUB_TARGET = function () {};
const stub = new Proxy(STUB_TARGET, {
  get(t, p) {
    if (p === Symbol.toPrimitive) return () => 0;
    if (p === Symbol.iterator) return function* () {};
    if (p === 'then' || p === 'toJSON' || p === 'nodeType') return undefined;
    if (p === 'length') return 0;
    if (p === 'toString' || p === 'valueOf') return () => '';
    return stub;
  },
  apply() { return stub; },
  construct() { return stub; },
  set() { return true; },
  has() { return true; },
});

// ------------------------------------------------------------- events
class Event {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = !!init.bubbles;
    this.cancelable = init.cancelable !== false;
    this.defaultPrevented = false;
    this._stop = false;
    this.target = null;
    this.currentTarget = null;
    this.timeStamp = clock.now;
    Object.assign(this, init);
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; this._stopNow = true; }
  composedPath() { const p = []; let n = this.target; while (n) { p.push(n); n = n.parentNode; } return p; }
}
const KEYCODES = { Enter: 13, Escape: 27, ' ': 32, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Backspace: 8, Tab: 9 };

class EventTargetImpl {
  constructor() { this._listeners = {}; this._on = {}; }
  addEventListener(type, fn, opts) {
    if (!fn) return;
    (this._listeners[type] = this._listeners[type] || []).push({ fn, once: !!(opts && opts.once) });
  }
  removeEventListener(type, fn) {
    const l = this._listeners[type]; if (!l) return;
    this._listeners[type] = l.filter(x => x.fn !== fn);
  }
  _hasListeners(type) {
    return !!((this._listeners[type] && this._listeners[type].length) || this._on[type] || (this._attrs && this._attrs['on' + type]));
  }
  dispatchEvent(ev) {
    if (!ev.target) ev.target = this;
    const path = [];
    let n = this;
    while (n) { path.push(n); n = n.parentNode || (n === documentImpl ? win : null); }
    for (const node of (ev.bubbles ? path : [this])) {
      ev.currentTarget = node;
      node._fire(ev);
      if (ev._stop) break;
    }
    return !ev.defaultPrevented;
  }
  _fire(ev) {
    const inline = this._inlineHandler && this._inlineHandler(ev.type);
    if (inline) callUser(inline, this, [ev], `on${ev.type} attribute`);
    const prop = this._on[ev.type];
    if (typeof prop === 'function') callUser(prop, this, [ev], `on${ev.type} handler`);
    const l = (this._listeners[ev.type] || []).slice();
    for (const { fn, once } of l) {
      if (once) this.removeEventListener(ev.type, fn);
      if (typeof fn === 'function') callUser(fn, this, [ev], `${ev.type} listener`);
      else if (fn && typeof fn.handleEvent === 'function') callUser(fn.handleEvent, fn, [ev], `${ev.type} listener`);
      if (ev._stopNow) break;
    }
  }
}

// ------------------------------------------------------------- DOM
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
let documentImpl = null;

class TextNode {
  constructor(text) { this.nodeType = 3; this.nodeName = '#text'; this.data = String(text); this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  get nodeValue() { return this.data; }
  set nodeValue(v) { this.data = String(v); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  cloneNode() { return new TextNode(this.data); }
}

function makeStyle() {
  const data = {};
  const api = {
    setProperty(k, v) { data[k] = String(v); }, getPropertyValue(k) { return data[k] || ''; },
    removeProperty(k) { const v = data[k]; delete data[k]; return v || ''; },
    get cssText() { return Object.entries(data).map(([k, v]) => `${k}: ${v}`).join('; '); },
    set cssText(v) { String(v).split(';').forEach(d => { const [k, ...r] = d.split(':'); if (k && r.length) data[k.trim()] = r.join(':').trim(); }); },
  };
  return new Proxy(data, {
    get(t, p) { if (p in api) return api[p]; if (typeof p === 'symbol') return undefined; return p in t ? t[p] : ''; },
    set(t, p, v) { t[p] = v == null ? '' : String(v); return true; },
  });
}

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  _set(a) { this.el.setAttribute('class', a.join(' ')); }
  add(...c) { const a = this._get(); c.forEach(x => { if (!a.includes(x)) a.push(x); }); this._set(a); }
  remove(...c) { this._set(this._get().filter(x => !c.includes(x))); }
  toggle(c, force) { const has = this.contains(c); const want = force === undefined ? !has : !!force; if (want && !has) this.add(c); if (!want && has) this.remove(c); return want; }
  contains(c) { return this._get().includes(c); }
  replace(a, b) { if (!this.contains(a)) return false; this._set(this._get().map(x => x === a ? b : x)); return true; }
  item(i) { return this._get()[i] || null; }
  get length() { return this._get().length; }
  get value() { return this._get().join(' '); }
  toString() { return this.value; }
  forEach(fn) { this._get().forEach(fn); }
  [Symbol.iterator]() { return this._get()[Symbol.iterator](); }
}

function describe(el) {
  if (!el || !el.tagName) return 'element';
  const tag = el.tagName.toLowerCase();
  const id = el.getAttribute('id');
  if (id) return `${tag}#${id}`;
  const txt = (el.textContent || '').trim().replace(/\s+/g, ' ');
  if (txt && txt.length <= 24) return `${tag} "${txt}"`;
  const cls = el.getAttribute('class');
  if (cls) return `${tag}.${cls.split(/\s+/)[0]}`;
  const val = el.getAttribute('value');
  if (val) return `${tag} "${val}"`;
  return tag;
}

class ElementImpl extends EventTargetImpl {
  constructor(tag, attrs = {}, line = 0, file = '') {
    super();
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.localName = String(tag).toLowerCase();
    this._attrs = Object.assign({}, attrs);
    this.childNodes = [];
    this.parentNode = null;
    this._line = line; this._file = file;
    this.style = makeStyle();
    if (attrs.style) this.style.cssText = attrs.style;
    this.classList = new ClassList(this);
    this._value = attrs.value !== undefined ? String(attrs.value) : undefined;
    this._checked = 'checked' in attrs;
    this.scrollTop = 0; this.scrollLeft = 0;
    this._ctx2d = null;
    this._compiled = {};
  }
  // -- attributes
  getAttribute(n) { n = String(n).toLowerCase(); return n in this._attrs ? String(this._attrs[n]) : null; }
  setAttribute(n, v) { n = String(n).toLowerCase(); this._attrs[n] = String(v); if (n === 'value') this._value = String(v); }
  removeAttribute(n) { delete this._attrs[String(n).toLowerCase()]; }
  hasAttribute(n) { return String(n).toLowerCase() in this._attrs; }
  toggleAttribute(n, f) { const has = this.hasAttribute(n); const want = f === undefined ? !has : !!f; if (want) this.setAttribute(n, ''); else this.removeAttribute(n); return want; }
  get attributes() { return Object.entries(this._attrs).map(([name, value]) => ({ name, value })); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get(t, p) { if (typeof p !== 'string') return undefined; const v = el.getAttribute('data-' + p.replace(/[A-Z]/g, m => '-' + m.toLowerCase())); return v === null ? undefined : v; },
      set(t, p, v) { el.setAttribute('data-' + String(p).replace(/[A-Z]/g, m => '-' + m.toLowerCase()), v); return true; },
      has(t, p) { return el.hasAttribute('data-' + String(p)); },
    });
  }
  // -- form-ish properties
  get value() { if (this._value !== undefined) return this._value; if (this.localName === 'textarea') return this.textContent; if (this.localName === 'select') { const o = this.querySelectorAll('option'); const s = o.find(x => x.hasAttribute('selected')) || o[0]; return s ? (s.getAttribute('value') !== null ? s.getAttribute('value') : s.textContent) : ''; } return this.localName === 'input' ? '' : stub; }
  set value(v) { this._value = v == null ? '' : String(v); }
  get valueAsNumber() { return parseFloat(this.value); }
  set valueAsNumber(v) { this.value = String(v); }
  get checked() { return this._checked; }
  set checked(v) { this._checked = !!v; }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { this.toggleAttribute('disabled', !!v); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { this.toggleAttribute('hidden', !!v); }
  get type() { return this.getAttribute('type') || (this.localName === 'button' ? 'submit' : this.localName === 'input' ? 'text' : ''); }
  set type(v) { this.setAttribute('type', v); }
  get name() { return this.getAttribute('name') || ''; }
  set name(v) { this.setAttribute('name', v); }
  get href() { return this.getAttribute('href') || ''; }
  set href(v) { this.setAttribute('href', v); }
  get src() { return this.getAttribute('src') || ''; }
  set src(v) { this.setAttribute('src', v); if (this.localName === 'img') schedule(() => this.dispatchEvent(new Event('load')), 1); }
  get placeholder() { return this.getAttribute('placeholder') || ''; }
  set placeholder(v) { this.setAttribute('placeholder', v); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get width() { return Number(this.getAttribute('width') || (this.localName === 'canvas' ? 300 : 0)); }
  set width(v) { this.setAttribute('width', v); }
  get height() { return Number(this.getAttribute('height') || (this.localName === 'canvas' ? 150 : 0)); }
  set height(v) { this.setAttribute('height', v); }
  get selectedIndex() { const o = this.querySelectorAll('option'); const i = o.findIndex(x => (x.getAttribute('value') ?? x.textContent) === this.value); return i < 0 ? 0 : i; }
  set selectedIndex(i) { const o = this.querySelectorAll('option')[i]; if (o) this.value = o.getAttribute('value') ?? o.textContent; }
  get options() { return this.querySelectorAll('option'); }
  get form() { return this.closest('form'); }
  get files() { return []; }
  get naturalWidth() { return 100; } get naturalHeight() { return 100; } get complete() { return true; }
  get offsetWidth() { return 100; } get offsetHeight() { return 30; } get clientWidth() { return 100; } get clientHeight() { return 30; }
  get offsetLeft() { return 0; } get offsetTop() { return 0; } get scrollHeight() { return 30; } get scrollWidth() { return 100; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === documentImpl; }
  // -- tree
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  _sib(d, el) { if (!this.parentNode) return null; const s = el ? this.parentNode.children : this.parentNode.childNodes; return s[s.indexOf(this) + d] || null; }
  get nextSibling() { return this._sib(1, false); } get previousSibling() { return this._sib(-1, false); }
  get nextElementSibling() { return this._sib(1, true); } get previousElementSibling() { return this._sib(-1, true); }
  _adopt(n) {
    if (n == null) return null;
    if (typeof n === 'string') n = new TextNode(n);
    if (n.nodeType === 11) { const kids = n.childNodes.slice(); kids.forEach(k => this._adopt(k)); return kids; }
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    return [n];
  }
  appendChild(n) { const k = this._adopt(n); if (k) this.childNodes.push(...k); return n; }
  append(...ns) { ns.forEach(n => this.appendChild(typeof n === 'string' ? new TextNode(n) : n)); }
  prepend(...ns) { ns.reverse().forEach(n => this.insertBefore(typeof n === 'string' ? new TextNode(n) : n, this.firstChild)); }
  insertBefore(n, ref) { const k = this._adopt(n); if (!k) return n; const i = ref ? this.childNodes.indexOf(ref) : -1; if (i < 0) this.childNodes.push(...k); else this.childNodes.splice(i, 0, ...k); return n; }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null; } return n; }
  replaceChild(n, old) { this.insertBefore(n, old); this.removeChild(old); return old; }
  replaceChildren(...ns) { this.childNodes.forEach(c => { c.parentNode = null; }); this.childNodes = []; this.append(...ns); }
  replaceWith(...ns) { const p = this.parentNode; if (!p) return; ns.forEach(n => p.insertBefore(typeof n === 'string' ? new TextNode(n) : n, this)); p.removeChild(this); }
  before(...ns) { const p = this.parentNode; if (p) ns.forEach(n => p.insertBefore(typeof n === 'string' ? new TextNode(n) : n, this)); }
  after(...ns) { const p = this.parentNode; if (!p) return; const nx = this.nextSibling; ns.forEach(n => p.insertBefore(typeof n === 'string' ? new TextNode(n) : n, nx)); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  cloneNode(deep) { const c = makeElement(this.localName, Object.assign({}, this._attrs)); if (deep) this.childNodes.forEach(k => c.appendChild(k.cloneNode(true))); return c; }
  hasChildNodes() { return this.childNodes.length > 0; }
  // -- text / html
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) { this.childNodes.forEach(c => { c.parentNode = null; }); this.childNodes = []; if (v !== '' && v != null) this.appendChild(new TextNode(String(v))); }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get innerHTML() { return this.childNodes.map(serialize).join(''); }
  set innerHTML(v) { this.childNodes.forEach(c => { c.parentNode = null; }); this.childNodes = []; parseFragment(String(v == null ? '' : v), this); }
  get outerHTML() { return serialize(this); }
  insertAdjacentHTML(pos, html) { const tmp = makeElement('div'); parseFragment(String(html), tmp); const kids = tmp.childNodes.slice(); const p = String(pos).toLowerCase(); if (p === 'beforeend') kids.forEach(k => this.appendChild(k)); else if (p === 'afterbegin') kids.reverse().forEach(k => this.insertBefore(k, this.firstChild)); else if (p === 'beforebegin') this.before(...kids); else this.after(...kids); }
  insertAdjacentElement(pos, el) { this.insertAdjacentHTML(pos, ''); const p = String(pos).toLowerCase(); if (p === 'beforeend') this.appendChild(el); else if (p === 'afterbegin') this.insertBefore(el, this.firstChild); else if (p === 'beforebegin') this.before(el); else this.after(el); return el; }
  insertAdjacentText(pos, t) { this.insertAdjacentHTML(pos, ''); this.appendChild(new TextNode(t)); }
  // -- queries
  querySelector(sel) { const r = query(this, sel, true); return r === STUB_RESULT ? stubElement(sel) : (r[0] || null); }
  querySelectorAll(sel) { const r = query(this, sel, false); return nodeList(r === STUB_RESULT ? [] : r); }
  getElementsByClassName(c) { return nodeList(descendants(this).filter(e => e.classList.contains(c))); }
  getElementsByTagName(t) { t = String(t).toLowerCase(); return nodeList(descendants(this).filter(e => t === '*' || e.localName === t)); }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { const m = matchesSel(n, sel); if (m === null) return stubElement(sel); if (m) return n; n = n.parentNode; } return null; }
  matches(sel) { const m = matchesSel(this, sel); return m === null ? false : m; }
  // -- behaviour
  click() { if (this.disabled) return; const ev = new Event('click', { bubbles: true }); this.dispatchEvent(ev); if (!ev.defaultPrevented) defaultAction(this); }
  focus() { documentImpl.activeElement = this; } blur() { if (documentImpl.activeElement === this) documentImpl.activeElement = documentImpl.body; }
  select() {} scrollIntoView() {} scrollTo() {} scrollBy() {}
  getBoundingClientRect() { return { x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
  animate() { return { finished: Promise.resolve(), cancel() {}, play() {}, pause() {}, onfinish: null }; }
  play() { return Promise.resolve(); } pause() {} load() {}
  submit() { this.dispatchEvent(new Event('submit', { bubbles: true })); } reset() {}
  requestSubmit() { this.submit(); }
  checkValidity() { return true; } reportValidity() { return true; } setCustomValidity() {}
  getContext(kind) { if (this.localName !== 'canvas') return null; if (!this._ctx2d) this._ctx2d = makeCanvasContext(this, kind); return this._ctx2d; }
  toDataURL() { return 'data:image/png;base64,'; } toBlob(cb) { if (cb) schedule(() => cb(stub), 1); }
  attachShadow() { return this; }
  _inlineHandler(type) {
    const code = this._attrs['on' + type];
    if (!code) return null;
    if (!(type in this._compiled)) {
      try {
        const lineOff = Math.max(0, (this._line || 1) - 2);
        this._compiled[type] = new vm.Script(`(function(event){\n${code}\n})`, { filename: this._file || 'index.html', lineOffset: lineOff }).runInContext(ctx);
      } catch (e) {
        report('error', e, `on${type}="${short(code, 60)}" on ${describe(this)}`);
        this._compiled[type] = null;
      }
    }
    return this._compiled[type];
  }
}
// Every element is a Proxy around an ElementImpl, and methods always run with
// `this` = the proxy, so identity checks (indexOf, ===, contains) are consistent.
// el.onclick = fn is recorded as a handler; unknown properties read as a stub.
function wrapElement(el) {
  return new Proxy(el, {
    get(t, p, r) {
      if (typeof p === 'symbol' || p in t) return Reflect.get(t, p, r);
      if (p.startsWith('on')) return t._on[p.slice(2)] || null;
      if (p.startsWith('_') || p === 'then' || p === 'toJSON') return undefined;
      return stub;
    },
    set(t, p, v, r) {
      if (typeof p === 'string' && p.startsWith('on') && !(p in t)) { t._on[p.slice(2)] = v; return true; }
      if (!(p in t)) { Object.defineProperty(t, p, { value: v, writable: true, configurable: true, enumerable: true }); return true; }
      return Reflect.set(t, p, v, r);
    },
    getPrototypeOf() { return HTMLElementCtor.prototype; },
  });
}
const RAW = new WeakMap();
function makeElement(tag, attrs, line, file) {
  const raw = new ElementImpl(tag, attrs || {}, line || 0, file || '');
  const w = wrapElement(raw);
  RAW.set(w, raw);
  return w;
}
function stubElement(sel) { const e = makeElement('div', { 'data-stub': String(sel) }); return e; }
function nodeList(arr) { arr.item = i => arr[i] || null; return arr; }
function descendants(root) { const r = []; (function walk(n) { for (const c of n.childNodes) if (c.nodeType === 1) { r.push(c); walk(c); } })(root); return r; }
function serialize(n) {
  if (n.nodeType === 3) return n.data.replace(/</g, '&lt;');
  const a = Object.entries(n._attrs || {}).map(([k, v]) => ` ${k}="${String(v).replace(/"/g, '&quot;')}"`).join('');
  if (VOID.has(n.localName)) return `<${n.localName}${a}>`;
  return `<${n.localName}${a}>${n.childNodes.map(serialize).join('')}</${n.localName}>`;
}
function decodeEntities(s) { return s.replace(/&(amp|lt|gt|quot|#39|apos|nbsp|times|divide|minus|#(\d+)|#x([0-9a-f]+));/gi, (m, n, d, h) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", apos: "'", nbsp: ' ', times: '×', divide: '÷', minus: '−' }[n.toLowerCase()] || (d ? String.fromCharCode(+d) : h ? String.fromCharCode(parseInt(h, 16)) : m))); }
function parseFragment(html, parent) {
  const stack = [parent];
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+|<)/g;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[0].startsWith('<!--')) continue;
    if (m[1]) { const t = m[1].toLowerCase(); for (let i = stack.length - 1; i > 0; i--) if (stack[i].localName === t) { stack.length = i; break; } continue; }
    if (m[2]) {
      const attrs = {}; const ar = /([^\s=>\/]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))?/g; let a;
      while ((a = ar.exec(m[3] || ''))) attrs[a[1].toLowerCase()] = a[2] ? decodeEntities(a[2].replace(/^["']|["']$/g, '')) : '';
      const el = makeElement(m[2], attrs);
      top.appendChild(el);
      if (!VOID.has(el.localName) && !m[4]) stack.push(el);
      continue;
    }
    if (m[5]) top.appendChild(new TextNode(decodeEntities(m[5])));
  }
}

// ------------------------------------------------------------- selectors
const STUB_RESULT = Symbol('unsupported');
function splitTop(s, ch) { const r = []; let d = 0, q = '', cur = ''; for (const c of s) { if (q) { if (c === q) q = ''; cur += c; continue; } if (c === '"' || c === "'") { q = c; cur += c; continue; } if (c === '(' || c === '[') d++; if (c === ')' || c === ']') d--; if (c === ch && d === 0) { r.push(cur); cur = ''; } else cur += c; } r.push(cur); return r.map(x => x.trim()).filter(Boolean); }
function parseCompound(s) {
  const parts = []; const re = /^(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:([~^$*|]?=)\s*("[^"]*"|'[^']*'|[^\]\s]+))?\s*\]|:(first-child|last-child|checked|disabled|enabled|not\(([^)]*)\)|nth-child\((\d+)\)|first-of-type|last-of-type|empty)/y;
  let i = 0;
  while (i < s.length) {
    re.lastIndex = i; const m = re.exec(s);
    if (!m) return null;
    parts.push(m); i = re.lastIndex;
  }
  return parts;
}
function matchCompound(el, parts) {
  for (const m of parts) {
    if (m[1]) { if (m[1] !== '*' && el.localName !== m[1].toLowerCase()) return false; }
    else if (m[2]) { if (el.getAttribute('id') !== m[2]) return false; }
    else if (m[3]) { if (!el.classList.contains(m[3])) return false; }
    else if (m[4]) {
      const v = el.getAttribute(m[4]); if (v === null) return false;
      if (m[5]) { const want = m[6].replace(/^["']|["']$/g, ''); const op = m[5];
        if (op === '=' && v !== want) return false; if (op === '^=' && !v.startsWith(want)) return false;
        if (op === '$=' && !v.endsWith(want)) return false; if (op === '*=' && !v.includes(want)) return false;
        if (op === '~=' && !v.split(/\s+/).includes(want)) return false; if (op === '|=' && !(v === want || v.startsWith(want + '-'))) return false; }
    } else if (m[7]) {
      const p = m[7]; const sib = el.parentNode ? el.parentNode.children : [el];
      if (p === 'first-child' && sib[0] !== el) return false;
      if (p === 'last-child' && sib[sib.length - 1] !== el) return false;
      if (p === 'checked' && !el.checked) return false;
      if (p === 'disabled' && !el.disabled) return false;
      if (p === 'enabled' && el.disabled) return false;
      if (p === 'empty' && el.childNodes.length) return false;
      if (p.startsWith('nth-child') && sib[Number(m[9]) - 1] !== el) return false;
      if (p === 'first-of-type' && sib.filter(s => s.localName === el.localName)[0] !== el) return false;
      if (p === 'last-of-type') { const t = sib.filter(s => s.localName === el.localName); if (t[t.length - 1] !== el) return false; }
      if (p.startsWith('not(')) { const inner = parseCompound(m[8].trim()); if (!inner) return null; if (matchCompound(el, inner)) return false; }
    }
  }
  return true;
}
function compileSel(sel) {
  const groups = [];
  for (const g of splitTop(String(sel), ',')) {
    const toks = g.replace(/\s*>\s*/g, ' > ').split(/\s+/).filter(Boolean);
    const steps = []; let comb = ' ';
    for (const t of toks) { if (t === '>') { comb = '>'; continue; } const c = parseCompound(t); if (!c) return null; steps.push({ comb, c }); comb = ' '; }
    if (!steps.length) return null;
    groups.push(steps);
  }
  return groups;
}
function matchSteps(el, steps, i) {
  const r = matchCompound(el, steps[i].c); if (r === null) return null; if (!r) return false;
  if (i === 0) return true;
  if (steps[i].comb === '>') return el.parentNode && el.parentNode.nodeType === 1 ? matchSteps(el.parentNode, steps, i - 1) : false;
  let p = el.parentNode;
  while (p && p.nodeType === 1) { const m = matchSteps(p, steps, i - 1); if (m) return true; p = p.parentNode; }
  return false;
}
function matchesSel(el, sel) { const g = compileSel(sel); if (!g) return null; for (const steps of g) { const m = matchSteps(el, steps, steps.length - 1); if (m === null) return null; if (m) return true; } return false; }
function query(root, sel, first) {
  const g = compileSel(sel);
  if (!g) { out.warnings.push({ phase, message: `selector not simulated: ${sel}` }); return STUB_RESULT; }
  const res = [];
  for (const el of descendants(root)) {
    for (const steps of g) { const m = matchSteps(el, steps, steps.length - 1); if (m === null) return STUB_RESULT; if (m) { res.push(el); break; } }
    if (first && res.length) break;
  }
  if (first && !res.length) noteNull(`querySelector('${sel}')`);
  return res;
}
function noteNull(what) { const st = userFrame(new Error().stack); nullLookups.push({ what, where: st }); }

// ------------------------------------------------------------- canvas
function makeCanvasContext(canvas, kind) {
  const state = { canvas, fillStyle: '#000', strokeStyle: '#000', lineWidth: 1, font: '10px sans-serif', globalAlpha: 1, textAlign: 'start', textBaseline: 'alphabetic' };
  return new Proxy(state, {
    get(t, p) {
      if (p in t) return t[p];
      if (p === 'measureText') return (s) => ({ width: String(s).length * 6, actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2 });
      if (p === 'getImageData' || p === 'createImageData') return (x, y, w = 1, h = 1) => ({ width: w, height: h, data: new Uint8ClampedArray(Math.max(0, Math.min(4 * w * h, 4e6))) });
      if (p === 'createLinearGradient' || p === 'createRadialGradient' || p === 'createPattern' || p === 'createConicGradient') return () => ({ addColorStop() {} });
      if (p === 'getTransform') return () => ({ a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 });
      if (p === 'isPointInPath' || p === 'isPointInStroke') return () => false;
      if (p === 'getLineDash') return () => [];
      if (typeof p === 'symbol') return undefined;
      return () => undefined;      // drawing calls are no-ops
    },
    set(t, p, v) { t[p] = v; return true; },
  });
}

// ------------------------------------------------------------- timers
const clock = { now: 1000 };
let timerSeq = 1;
const timers = new Map();
function schedule(fn, delay, interval, args) { const id = timerSeq++; timers.set(id, { id, fn, at: clock.now + Math.max(0, Number(delay) || 0), interval: interval ? Math.max(1, Number(delay) || 1) : 0, args: args || [], runs: 0 }); return id; }
let rafQueue = [];
function runTimers(ms, label) {
  const until = clock.now + ms;
  let budget = 400;
  while (budget-- > 0 && Date.now() - T0 < BUDGET_MS) {
    let next = null;
    for (const t of timers.values()) if (!next || t.at < next.at) next = t;
    if (!next || next.at > until) break;
    clock.now = Math.max(clock.now, next.at);
    next.runs++;
    if (next.interval && next.runs < 6) next.at = clock.now + next.interval; else timers.delete(next.id);
    if (typeof next.fn === 'function') callUser(next.fn, win, next.args, `${label} (timer)`);
    else if (typeof next.fn === 'string') runCode(next.fn, 'timer string');
  }
  clock.now = Math.max(clock.now, until);
}
function runFrames(n, label) {
  for (let i = 0; i < n && rafQueue.length && Date.now() - T0 < BUDGET_MS; i++) {
    const q = rafQueue; rafQueue = [];
    clock.now += 16;
    for (const { fn } of q) callUser(fn, win, [clock.now], `${label} (animation frame)`);
  }
}

// ------------------------------------------------------------- errors
function short(s, n) { s = String(s).replace(/\s+/g, ' '); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function userFrame(stack) {
  if (!stack) return null;
  for (const line of String(stack).split('\n')) {
    const m = line.match(/\(?([^\s()]+):(\d+):(\d+)\)?\s*$/);
    if (m && PAGE_FILES.has(m[1])) return { file: m[1], line: Number(m[2]) };
  }
  return null;
}
const BROWSER_GLOBAL = /^(webkit|moz|ms)|Observer$|Event$|Element$|^(HTML|SVG|CSS|DOM|WebGL|Web|RTC|Media|Speech|Intl|Gamepad|Bluetooth|USB|Serial|Notification|IDB|Cache|ServiceWorker|Worker|Shared|Broadcast|Payment|Credential|Push|Sync|XR|Clipboard|Font|Image|Offscreen|Path2D|Touch|Pointer|Keyboard|Geolocation)/;
function report(kind, err, where) {
  if (Date.now() - T0 > BUDGET_MS * 2) return;
  const name = (err && err.name) || 'Error';
  const msg = (err && err.message !== undefined) ? String(err.message) : String(err);
  const frame = userFrame(err && err.stack);
  if (name === 'ReferenceError') {
    const m = msg.match(/^(\S+) is not defined/);
    if (m && (BROWSER_GLOBAL.test(m[1]) || (input.remote_scripts && /^[A-Z$_]/.test(m[1])))) {
      out.warnings.push({ phase, message: `${name}: ${msg} (browser API or library not simulated)` });
      return;
    }
  }
  if (!frame && err && err.stack && /domsim\.js/.test(err.stack) && name !== 'ReferenceError') {
    out.warnings.push({ phase, message: `${name}: ${msg} (inside the simulator, ignored)` });
    return;
  }
  const hint = nullLookups.length && /null/.test(msg) ? nullLookups.slice(-3).map(n => `${n.what} returned null`).join('; ') : '';
  const entry = { phase: where || phase, type: name, message: msg, file: frame ? frame.file : null, line: frame ? frame.line : null, hint };
  const key = `${entry.type}|${entry.message}|${entry.file}|${entry.line}`;
  if (!out.errors.some(e => `${e.type}|${e.message}|${e.file}|${e.line}` === key)) out.errors.push(entry);
}
function callUser(fn, thisArg, args, where) {
  const prev = phase; phase = where || phase;
  nullLookups = [];
  try {
    ctx.__tc_fn = fn; ctx.__tc_this = thisArg; ctx.__tc_args = args || [];
    const r = CALL.runInContext(ctx, { timeout: 1500 });
    if (r && typeof r.then === 'function') r.then(undefined, e => report('error', e, where));
  } catch (e) {
    if (e && e.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT') out.errors.push({ phase: where, type: 'Timeout', message: 'took longer than 1.5s (infinite loop?)', file: null, line: null, hint: '' });
    else report('error', e, where);
  } finally { phase = prev; }
}
function runCode(code, where, filename, lineOffset) {
  try { new vm.Script(code, { filename: filename || 'inline', lineOffset: lineOffset || 0 }).runInContext(ctx, { timeout: 2000 }); }
  catch (e) {
    if (e && e.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT') out.errors.push({ phase: where, type: 'Timeout', message: 'script took longer than 2s to run (infinite loop?)', file: filename, line: null, hint: '' });
    else report('error', e, where);
  }
}

// ------------------------------------------------------------- window
const storage = () => { const d = new Map(); return { getItem: k => d.has(String(k)) ? d.get(String(k)) : null, setItem: (k, v) => { d.set(String(k), String(v)); }, removeItem: k => { d.delete(String(k)); }, clear: () => d.clear(), key: i => [...d.keys()][i] || null, get length() { return d.size; } }; };
class FakeObserver { constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }
const HTMLElementCtor = function HTMLElement() {};
const sandbox = {
  console: { log() {}, info() {}, debug() {}, table() {}, group() {}, groupEnd() {}, time() {}, timeEnd() {}, trace() {}, dir() {},
    warn() {}, assert(c, ...m) { if (!c) out.warnings.push({ phase, message: 'console.assert failed: ' + m.join(' ') }); },
    error(...a) { out.warnings.push({ phase, message: 'console.error: ' + short(a.map(x => x && x.message ? x.message : String(x)).join(' '), 160) }); } },
  setTimeout: (fn, d, ...a) => schedule(fn, d, false, a), setInterval: (fn, d, ...a) => schedule(fn, d, true, a),
  clearTimeout: id => { timers.delete(id); }, clearInterval: id => { timers.delete(id); },
  requestAnimationFrame: fn => { const id = timerSeq++; rafQueue.push({ id, fn }); return id; },
  cancelAnimationFrame: id => { rafQueue = rafQueue.filter(x => x.id !== id); },
  requestIdleCallback: fn => schedule(() => fn({ timeRemaining: () => 10, didTimeout: false }), 1), cancelIdleCallback: id => timers.delete(id),
  queueMicrotask, structuredClone, atob, btoa, URL, URLSearchParams, TextEncoder, TextDecoder, AbortController,
  crypto: globalThis.crypto, Intl, WeakRef,
  alert: (m) => { out.trace.push({ phase, alert: short(m, 120) }); }, confirm: () => true, prompt: (m, d) => (d !== undefined ? String(d) : '5'),
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve(''), blob: () => Promise.resolve(stub), headers: { get: () => null } }),
  XMLHttpRequest: function () { return stub; }, WebSocket: function () { return stub; }, EventSource: function () { return stub; },
  Audio: function (src) { const a = makeElement('audio', src ? { src } : {}); return a; },
  Image: function (w, h) { const i = makeElement('img', {}); if (w) i.width = w; if (h) i.height = h; return i; },
  AudioContext: function () { return stub; }, webkitAudioContext: function () { return stub; }, OfflineAudioContext: function () { return stub; },
  MutationObserver: FakeObserver, ResizeObserver: FakeObserver, IntersectionObserver: FakeObserver, PerformanceObserver: FakeObserver,
  Event, CustomEvent: class extends Event { constructor(t, i = {}) { super(t, i); this.detail = i.detail; } },
  KeyboardEvent: Event, MouseEvent: Event, PointerEvent: Event, TouchEvent: Event, InputEvent: Event, FocusEvent: Event, WheelEvent: Event, SubmitEvent: Event, DragEvent: Event,
  HTMLElement: HTMLElementCtor, Element: HTMLElementCtor, Node: HTMLElementCtor, HTMLCanvasElement: HTMLElementCtor, HTMLInputElement: HTMLElementCtor, HTMLButtonElement: HTMLElementCtor, HTMLDivElement: HTMLElementCtor, HTMLImageElement: HTMLElementCtor, Text: TextNode, DocumentFragment: function () { return makeFragment(); },
  DOMParser: function () { return { parseFromString: (s) => { const d = makeElement('html'); parseFragment(String(s), d); return { body: d, documentElement: d, querySelector: q => d.querySelector(q), querySelectorAll: q => d.querySelectorAll(q), getElementById: id => d.querySelector('#' + id) }; } }; },
  FileReader: function () { return stub; }, Blob: function () { return stub; }, File: function () { return stub; }, FormData: function (form) { const d = new Map(); return { get: k => d.get(k) ?? null, set: (k, v) => d.set(k, v), append: (k, v) => d.set(k, v), has: k => d.has(k), entries: () => d.entries() }; },
  Notification: Object.assign(function () { return stub; }, { permission: 'default', requestPermission: () => Promise.resolve('default') }),
  localStorage: storage(), sessionStorage: storage(), indexedDB: stub, caches: stub, speechSynthesis: stub, SpeechSynthesisUtterance: function () { return stub; },
  navigator: { userAgent: 'Mozilla/5.0 (tinycode-sim)', language: 'en-US', languages: ['en-US'], platform: 'Linux', onLine: true, maxTouchPoints: 0, hardwareConcurrency: 4, clipboard: { writeText: () => Promise.resolve(), readText: () => Promise.resolve('') }, vibrate: () => true, geolocation: stub, mediaDevices: stub, serviceWorker: stub, share: () => Promise.resolve(), getGamepads: () => [] },
  location: { href: 'http://localhost/index.html', origin: 'http://localhost', protocol: 'http:', host: 'localhost', hostname: 'localhost', port: '', pathname: '/index.html', search: '', hash: '', reload() {}, assign() {}, replace() {} },
  history: { length: 1, state: null, pushState() {}, replaceState() {}, back() {}, forward() {}, go() {} },
  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, orientation: { type: 'landscape-primary', angle: 0, addEventListener() {} } },
  innerWidth: 1280, innerHeight: 800, outerWidth: 1280, outerHeight: 800, devicePixelRatio: 1, scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
  matchMedia: (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  getComputedStyle: (el) => (el && el.style) || makeStyle(), getSelection: () => stub,
  scrollTo() {}, scrollBy() {}, scroll() {}, open() { return null; }, close() {}, print() {}, focus() {}, blur() {}, postMessage() {}, stop() {},
  performance: { now: () => clock.now, mark() {}, measure() {}, getEntriesByName: () => [], timeOrigin: 0 },
  CSS: { supports: () => true, escape: s => s }, Path2D: function () { return stub; }, ImageData: function (w, h) { return { width: w, height: h, data: new Uint8ClampedArray(4 * w * h) }; },
  OffscreenCanvas: function (w, h) { const c = makeElement('canvas', { width: w, height: h }); return c; },
  createImageBitmap: () => Promise.resolve(stub),
};
const winTarget = new EventTargetImpl();
for (const k of ['addEventListener', 'removeEventListener', 'dispatchEvent', '_fire', '_hasListeners']) sandbox[k] = winTarget[k].bind(winTarget);
sandbox._listeners = winTarget._listeners; sandbox._on = winTarget._on;
const ctx = vm.createContext(sandbox, { name: 'tinycode-app' });
const win = vm.runInContext('this', ctx);
sandbox.window = win; sandbox.self = win; sandbox.globalThis = win; sandbox.top = win; sandbox.parent = win; sandbox.frames = win;
const CALL = new vm.Script('__tc_fn.apply(__tc_this, __tc_args)', { filename: 'tinycode-call' });
// window.onload = fn / window.onkeydown = fn
for (const evName of ['load', 'DOMContentLoaded', 'keydown', 'keyup', 'keypress', 'resize', 'click', 'mousemove', 'mousedown', 'mouseup', 'beforeunload', 'scroll', 'blur', 'focus', 'touchstart', 'touchend']) {
  Object.defineProperty(win, 'on' + evName, { get: () => winTarget._on[evName] || null, set: (v) => { winTarget._on[evName] = v; }, configurable: true });
}

// ------------------------------------------------------------- document
function makeFragment() { const f = makeElement('#fragment'); RAW.get(f).nodeType = 11; return f; }
function buildDocument() {
  const docRaw = new ElementImpl('#document');
  const doc = wrapElement(docRaw);
  RAW.set(doc, docRaw);
  documentImpl = doc;
  docRaw.nodeType = 9;
  const nodes = input.nodes;
  const made = nodes.map(n => makeElement(n.t, n.a, n.l, input.html_file));
  nodes.forEach((n, i) => {
    for (const c of n.c) {
      if (typeof c === 'string') made[i].appendChild(new TextNode(c));
      else made[i].appendChild(made[c]);
    }
  });
  let html = made.find((e, i) => nodes[i].t === 'html' && nodes[i].p === -1);
  if (!html) { html = makeElement('html'); }
  const roots = nodes.map((n, i) => (n.p === -1 ? made[i] : null)).filter(Boolean);
  for (const r of roots) if (r !== html) html.appendChild(r);
  doc.appendChild(html);
  let head = html.querySelector('head'); if (!head) { head = makeElement('head'); html.insertBefore(head, html.firstChild); }
  let body = html.querySelector('body'); if (!body) { body = makeElement('body'); html.appendChild(body); }
  const api = {
    documentElement: html, head, body, activeElement: body, readyState: 'loading', title: (head.querySelector('title') || { textContent: '' }).textContent,
    cookie: '', referrer: '', URL: sandbox.location.href, hidden: false, visibilityState: 'visible', defaultView: win, location: sandbox.location,
    getElementById(id) { const r = descendants(doc).find(e => e.getAttribute('id') === String(id)) || null; if (!r) noteNull(`document.getElementById('${id}')`); return r; },
    getElementsByName(n) { return nodeList(descendants(doc).filter(e => e.getAttribute('name') === String(n))); },
    createElement: (t) => makeElement(String(t).toLowerCase()), createElementNS: (ns, t) => makeElement(String(t).toLowerCase()),
    createTextNode: (t) => new TextNode(t), createDocumentFragment: makeFragment, createComment: () => new TextNode(''),
    createEvent: () => new Event(''), hasFocus: () => true, execCommand: () => true, elementFromPoint: () => body,
    exitFullscreen: () => Promise.resolve(), fullscreenElement: null, fonts: { ready: Promise.resolve(), load: () => Promise.resolve([]) },
  };
  for (const [k, v] of Object.entries(api)) Object.defineProperty(docRaw, k, { value: v, writable: true, configurable: true });
  Object.defineProperty(docRaw, 'title', { get: () => api.title, set: (v) => { api.title = String(v); }, configurable: true });
  sandbox.document = doc;
  return doc;
}

// ------------------------------------------------------------- simulation
function defaultAction(el) {
  const raw = el;
  const tag = raw.localName;
  if (tag === 'input' && (raw.type === 'checkbox' || raw.type === 'radio')) { raw._checked = raw.type === 'radio' ? true : !raw._checked; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }
  const form = raw.closest && raw.closest('form');
  if (form && ((tag === 'button' && raw.type === 'submit') || (tag === 'input' && raw.type === 'submit'))) {
    const ev = new Event('submit', { bubbles: true, cancelable: true }); ev.submitter = el;
    form.dispatchEvent(ev);
  }
}
function snapshot(doc) {
  const s = new Map();
  for (const e of descendants(doc)) {
    const raw = e;
    if (['script', 'style', 'head', 'title', 'html', 'body'].includes(raw.localName)) continue;
    const own = raw.childNodes.filter(c => c.nodeType === 3).map(c => c.data).join('').trim();
    const val = (raw.localName === 'input' || raw.localName === 'textarea' || raw.localName === 'output') ? String(raw._value ?? '') : null;
    s.set(e, { text: own, value: val, desc: describe(e) });
  }
  return s;
}
const BAD_TEXT = /^(NaN|undefined|null|\[object Object\]|-?Infinity|function .*|\[object \w+\])$|(^|\s)(NaN|undefined)(\s|$)/;
function diffAndCheck(before, after, action, strict) {
  const changes = [];
  for (const [el, a] of after) {
    const b = before.get(el);
    for (const field of ['text', 'value']) {
      const nv = a[field]; if (nv === null || nv === undefined) continue;
      const ov = b ? b[field] : null;
      if (nv !== ov) {
        changes.push(`${a.desc}${field === 'value' ? '.value' : ''} = "${short(nv, 40)}"`);
        if (BAD_TEXT.test(nv.trim())) {
          const e = { phase: action, type: 'BadOutput', message: `${a.desc} shows "${short(nv, 40)}"`, file: null, line: null, hint: 'a calculation or variable produced an invalid value' };
          if (strict) out.errors.push(e); else out.warnings.push({ phase: action, message: `${e.message} (after clicking things in page order)` });
        }
      }
    }
  }
  if (changes.length) out.trace.push({ phase: action, changes: changes.slice(0, 6) });
}
const flush = () => new Promise(r => setImmediate(r));
async function settle(label) { await flush(); runTimers(50, label); runFrames(2, label); await flush(); }
async function act(doc, label, fn, strict = true) {
  const before = snapshot(doc);
  phase = label;
  try { fn(); } catch (e) { report('error', e, label); }
  await settle(label);
  diffAndCheck(before, snapshot(doc), label, strict);
}
const crashed = () => out.errors.some(e => e.type !== 'WrongResult' && e.type !== 'BadOutput');
function clickables(doc) {
  const r = [];
  for (const e of descendants(doc)) {
    const raw = e;
    if (raw.disabled) continue;
    const t = raw.localName;
    const isBtn = t === 'button' || (t === 'input' && ['button', 'submit', 'checkbox', 'radio', 'reset'].includes(raw.type)) || t === 'a';
    let listened = raw._hasListeners('click') || raw._hasListeners('mousedown') || raw._hasListeners('pointerdown');
    if (isBtn || listened) r.push(e);
  }
  return r;
}
function findTarget(doc, sel) {
  sel = String(sel);
  if (/^[#.\[]|^[a-z][\w-]*([#.\[:\s>]|$)/i.test(sel) && compileSel(sel)) {
    const r = query(doc, sel, true);
    if (r !== STUB_RESULT && r[0]) return r[0];
  }
  const want = sel.trim().toLowerCase();
  const cands = descendants(doc).filter(e => { const raw = e; return ['button', 'a', 'input', 'div', 'span', 'td', 'li'].includes(raw.localName); });
  const byText = cands.filter(e => { const raw = e; const txt = (raw.textContent || raw.getAttribute('value') || '').trim().toLowerCase(); return txt === want || raw.getAttribute('aria-label') === sel || raw.getAttribute('data-value') === sel || raw.getAttribute('data-key') === sel; });
  const prefer = byText.find(e => e.localName === 'button') || byText[0];
  return prefer || null;
}
function pressKey(key) {
  const init = { key, code: key.length === 1 ? (/\d/.test(key) ? 'Digit' + key : key === ' ' ? 'Space' : 'Key' + key.toUpperCase()) : key, keyCode: KEYCODES[key] || key.toUpperCase().charCodeAt(0), which: KEYCODES[key] || key.toUpperCase().charCodeAt(0), bubbles: true, cancelable: true, repeat: false, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false };
  const target = documentImpl.activeElement || documentImpl.body;
  for (const t of ['keydown', 'keypress', 'keyup']) { const ev = new Event(t, init); ev.target = target; target.dispatchEvent(ev); }
}
function textOf(el) { if (!el) return ''; const raw = el; const v = raw._value; if ((raw.localName === 'input' || raw.localName === 'textarea' || raw.localName === 'output') && v !== undefined) return String(v); return String(raw.textContent || ''); }

// --- automatic behaviour probes for very common small apps
function lastNumber(s) { const m = String(s).replace(/,/g, '').match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?(?!.*\d)/i); return m ? parseFloat(m[0]) : NaN; }
function findDisplay(doc) {
  const all = descendants(doc);
  const named = all.filter(e => /display|result|screen|output|answer|calc-?out|current|readout/i.test((e.getAttribute('id') || '') + ' ' + (e.getAttribute('class') || '')));
  const visible = named.filter(e => !['button', 'script', 'style'].includes(e.localName));
  return visible.find(e => e.localName === 'input') || visible[visible.length - 1] || null;
}
async function calcProbe(doc) {
  const clickable = new Set(clickables(doc));
  const btn = (labels) => {
    for (const l of labels) {
      const want = String(l).toLowerCase();
      const hit = [...clickable].find(e => {
        const t = (e.textContent || e.getAttribute('value') || '').trim().toLowerCase();
        return t === want || e.getAttribute('data-value') === l || e.getAttribute('data-key') === l || e.getAttribute('aria-label') === l;
      });
      if (hit) return hit;
    }
    return null;
  };
  const digits = '0123456789'.split('').map(d => btn([d]));
  if (digits.some(d => !d)) return;
  const ops = { '+': btn(['+']), '-': btn(['-', '−', '–']), '*': btn(['×', '*', 'x', '✕']), '/': btn(['÷', '/']), '=': btn(['=']) };
  const clear = btn(['C', 'AC', 'CE', 'Clear', 'clr']);
  const display = findDisplay(doc);
  if (!ops['+'] || !ops['='] || !display) return;
  let cases = [['2+3=', 5], ['9-4=', 5], ['6*7=', 42], ['8/2=', 4], ['12+30=', 42]];
  if (!clear) cases = cases.slice(0, 1);          // can't reset between cases
  const pretty = k => (k === '*' ? '×' : k === '/' ? '÷' : k);
  for (const [seq, want] of cases) {
    const keys = seq.split('');
    if (keys.some(k => !(/\d/.test(k) ? digits[+k] : ops[k]))) continue;
    if (clear) await act(doc, 'calculator probe: clear', () => clear.click());
    const errorsBefore = out.errors.length;
    const label = keys.map(pretty).join(' ');
    for (const k of keys) await act(doc, `calculator probe: ${label} (pressing "${pretty(k)}")`, () => (/\d/.test(k) ? digits[+k] : ops[k]).click());
    const shown = textOf(display).trim();
    let ok = Math.abs(lastNumber(shown) - want) < 1e-9;
    if (!ok) {
      // maybe we guessed the wrong display: accept the answer shown anywhere
      ok = descendants(doc).some(e => !['button', 'script', 'style'].includes(e.localName) &&
        e.childNodes.some(c => c.nodeType === 3 && Math.abs(lastNumber(c.data) - want) < 1e-9 && /\d/.test(c.data)) ||
        (e.localName === 'input' && Math.abs(lastNumber(e._value ?? '') - want) < 1e-9));
    }
    out.probes.push({ name: `calculator: ${label}`, ok, expected: String(want), got: shown, display: describe(display) });
    if (!ok && out.errors.length === errorsBefore && !crashed()) out.errors.push({ phase: 'calculator probe', type: 'WrongResult', message: `pressing ${label} shows "${short(shown, 40)}" in ${describe(display)} — expected ${want}`, file: null, line: null, hint: '' });
  }
  if (clear) await act(doc, 'calculator probe: clear', () => clear.click());
}
async function todoProbe(doc) {
  const inputs = descendants(doc).filter(e => (e.localName === 'input' && ['text', '', 'search'].includes(e.getAttribute('type') || '')) || e.localName === 'textarea');
  const lists = descendants(doc).filter(e => ['ul', 'ol'].includes(e.localName) || /list|todo|items|tasks/i.test((e.getAttribute('id') || '') + ' ' + (e.getAttribute('class') || '')));
  if (inputs.length !== 1 || !lists.length) return;
  const inp = inputs[0];
  const form = inp.closest('form');
  const addBtn = clickables(doc).find(e => /^(add|\+|add task|add item|add todo|create|save|submit)$/i.test((e.textContent || e.getAttribute('value') || '').trim()) || /add|create/i.test(e.getAttribute('id') || ''));
  if (!addBtn && !form) return;                  // not clearly an "add item" app
  const item = 'tinycode probe item';
  await act(doc, 'todo probe: type an item', () => { inp.focus(); inp.value = item; inp.dispatchEvent(new Event('input', { bubbles: true })); });
  await act(doc, 'todo probe: add it', () => {
    if (addBtn) addBtn.click();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  const ok = descendants(doc).some(e => e !== inp && e.childNodes.some(c => c.nodeType === 3 && c.data.includes(item)));
  out.probes.push({ name: 'todo: add an item', ok, expected: item, got: ok ? 'added' : 'not found in the page', display: lists.map(describe).join(', ') });
  if (!ok && !crashed()) out.errors.push({ phase: 'todo probe', type: 'WrongResult', message: `typing "${item}" into ${describe(inp)} and ${addBtn ? `clicking ${describe(addBtn)}` : 'submitting the form'} did not add it to the page`, file: null, line: null, hint: '' });
}

// ------------------------------------------------------------- main
process.on('unhandledRejection', e => report('error', e, phase));
async function main() {
const doc = buildDocument();
for (const s of input.scripts) {
  if (Date.now() - T0 > BUDGET_MS) break;
  if (s.skip) { out.skipped.push(s.skip); continue; }
  phase = `running ${s.file}`;
  runCode(s.code, `running ${s.file}${s.inline ? ` (inline <script> at line ${s.line})` : ''}`, s.file, s.inline ? s.line - 1 : 0);
  out.ran.push(s.file + (s.inline ? `:${s.line}` : ''));
  await flush();
  runTimers(0, 'startup');
}
RAW.get(doc).readyState = 'interactive';
phase = 'DOMContentLoaded';
doc.dispatchEvent(new Event('DOMContentLoaded', { bubbles: true }));
RAW.get(doc).readyState = 'complete';
phase = 'load';
win.dispatchEvent(new Event('load'));
await flush(); runTimers(1100, 'startup'); runFrames(3, 'startup'); await flush();

if (input.steps && input.steps.length) {
  // scenario written by the model: [{click}, {type:[sel,text]}, {key}, {wait}, {expect:[sel,text]}]
  for (const [i, st] of input.steps.entries()) {
    if (Date.now() - T0 > BUDGET_MS) break;
    const n = i + 1;
    if (st.click !== undefined) {
      const el = findTarget(doc, st.click);
      if (!el) { out.expects.push({ step: n, ok: false, message: `step ${n}: nothing to click matches "${st.click}"` }); continue; }
      await act(doc, `step ${n}: click ${describe(el)}`, () => el.click());
    } else if (st.type !== undefined) {
      const [sel, text] = Array.isArray(st.type) ? st.type : [Object.keys(st.type)[0], Object.values(st.type)[0]];
      const el = findTarget(doc, sel);
      if (!el) { out.expects.push({ step: n, ok: false, message: `step ${n}: no input matches "${sel}"` }); continue; }
      await act(doc, `step ${n}: type into ${describe(el)}`, () => { el.focus(); el.value = String(text); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); });
    } else if (st.key !== undefined) {
      await act(doc, `step ${n}: press ${st.key}`, () => pressKey(String(st.key)));
    } else if (st.wait !== undefined) {
      await act(doc, `step ${n}: wait ${st.wait}ms`, () => { runTimers(Math.min(Number(st.wait) || 0, 60000), 'wait'); runFrames(Math.ceil((Number(st.wait) || 0) / 16), 'wait'); });
    } else if (st.expect !== undefined) {
      const [sel, want] = Array.isArray(st.expect) ? st.expect : [Object.keys(st.expect)[0], Object.values(st.expect)[0]];
      const el = findTarget(doc, sel);
      const got = el ? textOf(el).trim() : null;
      const w = String(want).trim();
      const ok = got !== null && (got === w || (w !== '' && got.includes(w)) || (!isNaN(parseFloat(w)) && Math.abs(lastNumber(got) - parseFloat(w)) < 1e-9));
      out.expects.push({ step: n, ok, message: ok ? `step ${n}: ${describe(el)} shows "${short(got, 40)}" ✓` : (el ? `step ${n}: expected ${describe(el)} to show "${w}" but it shows "${short(got, 60)}"` : `step ${n}: no element matches "${sel}"`) });
    }
  }
} else {
  // generic smoke test: click everything once, press common keys
  const list = clickables(doc).slice(0, MAX_CLICKS);
  for (const el of list) {
    if (Date.now() - T0 > BUDGET_MS) { out.warnings.push({ phase: 'clicks', message: 'time budget reached; not every element was clicked' }); break; }
    if (!el.isConnected) continue;
    const raw = RAW.get(el) || el;
    if (raw.localName === 'a' && !raw._hasListeners('click') && !(raw.getAttribute('href') || '').startsWith('#')) continue;
    out.clicks = (out.clicks || 0) + 1;
    await act(doc, `clicking ${describe(el)}`, () => el.click(), false);
  }
  const keyTargets = [win, doc, doc.body];
  if (keyTargets.some(t => t._hasListeners && (t._hasListeners('keydown') || t._hasListeners('keyup') || t._hasListeners('keypress')))) {
    for (const k of ['ArrowLeft', 'ArrowRight', 'ArrowDown', 'ArrowUp', ' ', 'Enter', '1', '+', '=', 'Escape', 'Backspace', 'p']) {
      if (Date.now() - T0 > BUDGET_MS) break;
      await act(doc, `pressing key "${k === ' ' ? 'Space' : k}"`, () => pressKey(k), false);
    }
    await act(doc, 'game loop (2s)', () => { runTimers(2000, 'game loop'); runFrames(30, 'game loop'); }, false);
  }
  if (input.probes !== false) {
    try { await calcProbe(doc); } catch (e) { out.warnings.push({ phase: 'probe', message: String(e && e.message) }); }
    try { await todoProbe(doc); } catch (e) { out.warnings.push({ phase: 'probe', message: String(e && e.message) }); }
  }
}
}
main().catch(e => out.warnings.push({ phase: 'simulator', message: String(e && e.stack || e) })).finally(() => {
  out.elapsed_ms = Date.now() - T0;
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
});
