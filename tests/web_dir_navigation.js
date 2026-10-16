// Exercise the real native-picker template code with delayed HTTP and a minimal DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../pycut/templates/config.html'), 'utf8');
assert(template.includes('let picking=false;'), 'native directory handler must be present');
const navigation = 'let picking=false;' + template.split('let picking=false;')[1].split('</script>')[0];

function element(value = '') {
  return {value, isConnected: true, disabled: false, textContent: '', className: '', attributes: {}, events: [], focused: 0,
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    focus() { this.focused++; },
    dispatchEvent(event) { this.events.push(event); },
  };
}
function setup() {
  const fields = {first: element('/original'), second: element('/second-original')};
  const pending = [], buttons = [], status = element();
  const context = vm.createContext({status, loading: false, saving: false, token: 'session-token',
    form: {elements: fields, addEventListener() {}},
    document: {querySelectorAll() { return buttons; }},
    Event: class { constructor(type, options) { this.type = type; this.bubbles = options.bubbles; } },
    fetch(url, options) { return new Promise((resolve, reject) => pending.push({url, options, resolve, reject})); },
  });
  vm.runInContext(navigation, context);
  function button(input = null) {
    const result = element();
    result.closest = () => input ? {querySelector() { return input; }} : null;
    buttons.push(result);
    return result;
  }
  function respond(index, data, ok = true) { pending[index].resolve({ok, json: async () => data}); }
  const picking = () => vm.runInContext('picking', context);
  return {context, fields, pending, status, buttons, button, respond, picking};
}

(async () => {
  const t = setup(), c = t.context;
  const firstButton = t.button(), secondButton = t.button();
  let request = c.openBrowse(firstButton, 'first');
  assert.equal(t.picking(), true);
  assert(t.buttons.every(button => button.disabled));
  assert.equal(firstButton.attributes['aria-busy'], 'true');
  assert.equal(t.pending[0].url, '/api/pick-directory');
  assert.equal(t.pending[0].options.method, 'POST');
  assert.equal(t.pending[0].options.headers['X-Config-Token'], 'session-token');
  assert.deepEqual(JSON.parse(t.pending[0].options.body), {key: 'first', path: '/original'});
  await c.openBrowse(secondButton, 'second');
  assert.equal(t.pending.length, 1, 'a pending native dialog blocks another picker');
  t.respond(0, {path: '/中文 空格', cancelled: false});
  await request;
  assert.equal(t.fields.first.value, '/中文 空格');
  assert.equal(t.fields.second.value, '/second-original');
  assert.equal(t.fields.first.events.length, 1);
  assert.equal(t.fields.first.events[0].type, 'input');
  assert.equal(t.fields.first.events[0].bubbles, true);
  assert.equal(t.picking(), false);
  assert(t.buttons.every(button => !button.disabled));
  assert.equal(firstButton.focused, 1);

  request = c.openBrowse(secondButton, 'second');
  t.respond(1, {path: null, cancelled: true});
  await request;
  assert.equal(t.fields.second.value, '/second-original', 'native cancel must leave input unchanged');
  assert.equal(t.fields.second.events.length, 0);
  assert.match(t.status.textContent, /已取消选择/);

  request = c.openBrowse(secondButton, 'second');
  t.respond(2, {error: '没有图形桌面，请手动输入路径'}, false);
  await request;
  assert.equal(t.fields.second.value, '/second-original');
  assert.equal(t.status.className, 'error');
  assert.match(t.status.textContent, /手动输入/);
  assert.equal(t.picking(), false);

  request = c.openBrowse(firstButton, 'first');
  t.fields.first.value = '/manual-edit';
  t.respond(3, {path: '/late-result', cancelled: false});
  await request;
  assert.equal(t.fields.first.value, '/manual-edit', 'a late result must not overwrite manual editing');
  assert.equal(t.fields.first.events.length, 1);
  assert.match(t.status.textContent, /已忽略/);

  const rowInput = element('/row-two'), rowButton = t.button(rowInput);
  request = c.openBrowse(rowButton, 'first');
  rowInput.isConnected = false;
  rowButton.isConnected = false;
  const replacement = element('/replacement-row');
  t.fields.first = replacement;
  t.respond(4, {path: '/removed-row-result', cancelled: false});
  await request;
  assert.equal(rowInput.value, '/row-two', 'deleted row must not be written');
  assert.equal(replacement.value, '/replacement-row', 'replacement field must not receive an old row result');
  assert.equal(rowButton.focused, 0);

  const attachedRow = element('/attached-row'), attachedButton = t.button(attachedRow);
  request = c.openBrowse(attachedButton, 'first');
  t.respond(5, {path: '/selected-row', cancelled: false});
  await request;
  assert.equal(attachedRow.value, '/selected-row', 'native response binds to the specific multi-directory input');
  assert.equal(replacement.value, '/replacement-row', 'hidden group value is not a directory row');

  request = c.openBrowse(secondButton, 'second');
  t.pending[6].reject(new Error('Failed to fetch'));
  await request;
  assert.match(t.status.textContent, /连接中断/);
  assert.equal(t.picking(), false);
  assert.equal(secondButton.attributes['aria-busy'], undefined);

  c.loading = true;
  await c.openBrowse(secondButton, 'second');
  c.loading = false; c.saving = true;
  await c.openBrowse(secondButton, 'second');
  assert.equal(t.pending.length, 7, 'loading/saving cannot launch a native dialog');
  console.log('PASS: native POST/session, pending guard, cancel, errors, specific-row binding and edit/delete races');
})().catch(error => { console.error(error); process.exitCode = 1; });
