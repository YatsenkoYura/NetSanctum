// Builds the Vault sidebar by running the template's own code — `loadVaultSummary`
// included, not just the row renderer — against a stub DOM, and prints what came
// out as JSON.
//
// This exists because of bugs no text contract can see. The rows once rendered as
// `ws-item-undefined`, because `vaultCollectionsById` was filled from the API
// response without copying `id`: unclickable, an invalid drop target, and dragging
// a workspace complained that a sealed one cannot be merged. A fixture that
// supplies its own state would never catch that, so the state here comes from a
// stubbed fetch running the real builder.
//
// Usage: node vault_sidebar_dom.mjs <path-to-vault_dashboard.html>

import fs from 'node:fs';

const templatePath = process.argv[2];
if (!templatePath) {
  console.error('usage: node vault_sidebar_dom.mjs <template>');
  process.exit(2);
}
const html = fs.readFileSync(templatePath, 'utf8');

function makeElement(tag = 'div') {
  const element = {
    tagName: tag.toUpperCase(),
    children: [],
    parentNode: null,
    _listeners: {},
    dataset: {},
    style: {},
    value: '',
    classList: {
      _set: new Set(),
      add(...names) { names.forEach(n => this._set.add(n)); },
      remove(...names) { names.forEach(n => this._set.delete(n)); },
      contains(name) { return this._set.has(name); },
      toggle(name, on) { on ? this._set.add(name) : this._set.delete(name); },
    },
    set className(value) { this.classList._set = new Set(String(value).split(/\s+/).filter(Boolean)); },
    get className() { return [...this.classList._set].join(' '); },
    textContent: '',
    title: '',
    id: '',
    type: '',
    draggable: false,
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    appendChild(child) { this.append(child); },
    append(...kids) { kids.forEach(kid => { kid.parentNode = this; this.children.push(kid); }); },
    replaceChildren(...kids) { this.children = []; this.append(...kids); },
    getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
  };
  return element;
}

const elements = new Map();
globalThis.document = {
  createElement: makeElement,
  getElementById(id) {
    if (!elements.has(id)) {
      const element = makeElement();
      element.id = id;
      elements.set(id, element);
    }
    return elements.get(id);
  },
};
globalThis.window = { matchMedia: () => ({ matches: false }) };

// What the API answers: two plain spaces, one nested under another, one sealed.
const COLLECTIONS = [
  { id: 1, name: 'WOW', items_count: 0, is_encrypted: false, is_locked: false, parent_id: null, position: 0 },
  { id: 2, name: 'ДНД', items_count: 4, is_encrypted: false, is_locked: false, parent_id: null, position: 1024 },
  { id: 3, name: 'УЧЕБА', items_count: 0, is_encrypted: true, is_locked: true, parent_id: null, position: 2048 },
  { id: 4, name: 'ПОД', items_count: 2, is_encrypted: false, is_locked: false, parent_id: 2, position: 0 },
];

globalThis.requestJson = async (url) => {
  if (String(url).includes('/api/vault/collections')) return COLLECTIONS;
  if (String(url).includes('/api/vault/stats')) return { total_items: 6, avg_score: 7 };
  return null;
};
globalThis.packageUrl = url => url;
globalThis.packageMode = false;
globalThis.readOnlyMode = false;
globalThis.escapeHtml = value => String(value);
globalThis.showToast = () => {};
globalThis.currentCollectionId = null;
globalThis.vaultCollapsedSpaces = new Set();
globalThis.vaultCollectionsById = {};
globalThis.vaultChildIds = {};
globalThis.vaultExpandedStacks = {};
globalThis.currentItems = [];
globalThis.activeView = 'tiles';
globalThis.lockedGateId = null;
const selected = [];
globalThis.selectWorkspace = (id, name) => { selected.push({ id, name }); };

/** Pull one top-level function declaration out of the template, braces balanced.
 *  Keeps an `async` prefix: without it the body's `await` stops being legal and
 *  the extraction fails at parse time rather than at the assertion. */
function extract(name) {
  const anchor = html.indexOf(`function ${name}(`);
  if (anchor === -1) throw new Error(`not found in template: ${name}`);
  const start = html.slice(0, anchor).endsWith('async ') ? anchor - 6 : anchor;
  let depth = 0;
  for (let i = html.indexOf('{', anchor); i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}') { depth--; if (depth === 0) return html.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const NAMES = ['loadVaultSummary', 'renderChildSpaces', 'vaultSidebarRow', 'vaultSidebarRows', 'wsIdFromNode'];
const source = NAMES.map(extract).join('\n');
const scope = { ...globalThis };
// `new Function` bodies are not modules, so top-level await has to be wrapped.
const exported = new Function(
  ...Object.keys(scope),
  `return (async () => {\n${source}\nreturn { loadVaultSummary, vaultVaults: vaultSidebarRows, wsIdFromNode, map: () => vaultCollectionsById };\n})();`,
)(...Object.values(scope));

const api = await exported;
await api.loadVaultSummary();

const rows = elements.get('vault-collections').children;
rows.forEach(row => row._listeners.click[0]());

console.log(JSON.stringify({
  selected,
  records: Object.values(api.map()).map(record => ({ id: record.id, name: record.name, parent_id: record.parent_id })),
  rows: rows.map(row => ({
    id: row.id,
    parsedId: api.wsIdFromNode(row),
    depth: row.dataset.depth,
    draggable: row.draggable,
    hasTwisty: row.children.some(child => child.classList.contains('ws-twisty')),
    hasTwistySpacer: row.children.some(child => child.classList.contains('ws-twisty-spacer')),
    labelClasses: (row.children.find(child => child.classList.contains('items-center')) || {}).className || '',
  })),
}));
