// Ejecuta la página real con un DOM sintético que registra escrituras de HTML.
const vm = require('node:vm'), fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {}, writes = [];
let active = null;

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.events = {};
  }
  append(...parts) {
    for (const part of parts) {
      if (part instanceof Element) part.parentElement = this;
      this.children.push(part);
    }
  }
  prepend(part) {
    part.parentElement = this;
    this.children.unshift(part);
  }
  remove() {
    const parent = this.parentElement;
    parent.children.splice(parent.children.indexOf(this), 1);
  }
  focus() { active = this; }
  closest(selector) {
    if (selector !== '[data-tren-focus]') return null;
    for (let node = this; node instanceof Element; node = node.parentElement) {
      if (node.dataset.trenFocus != null) return node;
    }
    return null;
  }
  setAttribute(name, value) { this[name] = String(value); }
  addEventListener(event, handler) { this.events[event] = handler; }
  get textContent() {
    return this.children.map(child => child instanceof Element ? child.textContent : String(child)).join('');
  }
  set textContent(value) { this.children = [String(value)]; }
  get innerHTML() { return this.html ?? ''; }
  set innerHTML(value) {
    this.html = String(value);
    this.children = [];
    if (this.tagName === 'APP') {
      // El resto del rack conserva su plantilla. Sólo importa su posición en el árbol.
      if (this.html.includes('<div class="stack">')) {
        const stack = new Element('div');
        stack.className = 'stack';
        this.append(stack);
      }
    } else {
      writes.push(this.html);
    }
  }
}

const elementChildren = node => node.children.filter(child => child instanceof Element);
const tree = node => ({
  tag: node.tagName, cls: node.className, text: node.textContent, href: node.href,
  children: elementChildren(node).map(tree)
});
const descendants = node => [node, ...elementChildren(node).flatMap(descendants)];
const connected = node => {
  for (let at = node; at instanceof Element; at = at.parentElement) if (at === elements.app) return true;
  return false;
};
function moreButtons(node) {
  if (node.dataset.trenMore) return [node];
  return elementChildren(node).flatMap(moreButtons);
}

const context = {
  console, AbortSignal: {timeout() {}}, crypto: require('node:crypto').webcrypto,
  document: {
    getElementById: id => elements[id] ||= new Element(id === 'app' ? 'app' : 'div'),
    createElement: tag => new Element(tag),
    get activeElement() { return active; },
    addEventListener() {}, querySelector: () => null,
    querySelectorAll: selector => selector === '[data-tren-focus]'
      ? descendants(elements.app).filter(node => node.dataset.trenFocus != null) : []
  },
  window: {addEventListener() {}}, setInterval() {}, setTimeout() {}, clearTimeout() {},
  sessionStorage: {getItem() { return null; }, setItem() {}}, fetch: async () => ({ok: false}),
  fixture: input.fixture
};
vm.createContext(context);
vm.runInContext(input.js, context);
vm.runInContext('render(fixture)', context);
if (input.expand) {
  for (const button of moreButtons(elements.app)) button.events.click();
  vm.runInContext('paint()', context);
}
let focus = null;
if (input.focus) {
  // Un refresco con la misma instantánea no debe sacar el foco de la sección.
  if (input.focus === 'mas') moreButtons(elements.app)[0].events.click();
  else descendants(elements.app).find(node => node.tagName === 'A').focus();
  const before = active.dataset.trenFocus;
  vm.runInContext('paint()', context);
  focus = {before, after: active?.dataset.trenFocus, connected: connected(active), tag: active?.tagName};
}
const children = elementChildren(elements.app);
process.stdout.write(JSON.stringify({
  order: children.map(node => node.className),
  sections: children.filter(node => node.tagName === 'SECTION').map(tree),
  html: elements.app.innerHTML, writes, focus
}));
