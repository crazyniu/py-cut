// Test the template itself, with delayed HTTP responses and a minimal DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../pycut/templates/config.html'), 'utf8');
const navigation = template.split("const browseModal=")[1].split('</script>')[0];

function setup() {
  const elements = new Map();
  function element() {
    return {hidden: true, disabled: false, value: '', textContent: '', innerHTML: '',
      addEventListener() {}, appendChild() {}, focus() {}, dispatchEvent() {}};
  }
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement: element, querySelectorAll: () => [], addEventListener() {},
  };
  const pending = [];
  const fields = {first: element(), second: element()};
  fields.first.value = '/original';
  fields.second.value = '/second-original';
  const context = vm.createContext({document, form: {elements: fields}, labels: {}, token: 'test',
    Event: class {}, fetch: () => new Promise((resolve, reject) => pending.push({resolve, reject})),
  });
  vm.runInContext('const browseModal=' + navigation, context);
  const state = () => vm.runInContext('browse', context);
  const select = document.getElementById('browse-select');
  function respond(index, directory, error = '') {
    pending[index].resolve({ok: true, json: async () => ({
      path: directory, parent: '/', dirs: [], roots: [], error,
    })});
  }
  return {context, pending, fields, state, select, respond};
}

(async () => {
  const t = setup();
  const {context: c} = t;
  // A previous valid selection must become invalid immediately when navigation starts.
  c.openBrowse({focus() {}}, 'first');
  t.respond(0, '/original');
  await new Promise(resolve => setImmediate(resolve));
  const slow = c.browseEnter('/slow');
  assert.equal(t.state().valid, false, 'pending navigation must invalidate the old directory');
  assert.equal(t.select.disabled, true);
  c.closeBrowse(true);
  assert.equal(t.fields.first.value, '/original');

  // Reopen for another field; the old response cannot select a directory for it.
  c.openBrowse({focus() {}}, 'second');
  t.respond(2, '/中文 空格');
  await new Promise(resolve => setImmediate(resolve));
  t.respond(1, '/slow');
  await slow;
  assert.equal(t.state().path, '/中文 空格');
  c.closeBrowse(true);
  assert.equal(t.fields.second.value, '/中文 空格');

  c.openBrowse({focus() {}}, 'first');
  const latest = c.browseEnter('/latest');
  t.respond(4, '/latest');
  await latest;
  t.respond(3, '/stale');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(t.state().path, '/latest', 'late success must not overwrite the newest result');

  const staleFailure = c.browseEnter('/stale-failure');
  const success = c.browseEnter('/success');
  t.respond(6, '/success');
  await success;
  t.pending[5].reject(new Error('offline'));
  await staleFailure;
  assert.equal(t.state().valid, true, 'late failure must not invalidate a newer success');
  assert.equal(t.select.disabled, false);

  const failure = c.browseEnter('/missing');
  t.respond(7, '/missing', '目录不存在');
  await failure;
  assert.equal(t.select.disabled, true);
  c.closeBrowse(true);
  assert.equal(t.fields.first.value, '/original');

  c.openBrowse({focus() {}}, 'first');
  c.closeBrowse(false);
  t.respond(8, '/cancelled');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(t.state().valid, false, 'cancelled response must stay invalid');
  assert.equal(t.fields.first.value, '/original');
  console.log('PASS: pending selection, stale success/failure, field isolation and cancellation');
})().catch(error => { console.error(error); process.exitCode = 1; });
