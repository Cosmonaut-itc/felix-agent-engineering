// Ejecuta la página real con un DOM sintético que registra escrituras de HTML.
const vm = require('node:vm'), fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {}, writes = [];

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
  focus() {}
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
function moreButtons(node) {
  if (node.dataset.trenMore) return [node];
  return elementChildren(node).flatMap(moreButtons);
}

const context = {
  console, AbortSignal: {timeout() {}}, crypto: require('node:crypto').webcrypto,
  document: {
    getElementById: id => elements[id] ||= new Element(id === 'app' ? 'app' : 'div'),
    createElement: tag => new Element(tag),
    addEventListener() {}, querySelector: () => null, querySelectorAll: () => []
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
const children = elementChildren(elements.app);
process.stdout.write(JSON.stringify({
  order: children.map(node => node.className),
  sections: children.filter(node => node.tagName === 'SECTION').map(tree),
  html: elements.app.innerHTML, writes
}));
