// Logic and page wiring of FusionBulkExport/ui/summary.js; run with node (tests/test_tree_js.py does).
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const s = require('../FusionBulkExport/ui/summary.js');

// Zero rows are left out, except the first ("Exported now"); bad values become safe ones.
{
  const v = s.normalise({ headline: 'Finished', rows: [['Exported now', 0], ['Already up to date', 0], ['Failed', 2],
                                                       ['Bad', 'x'], 'junk'], more: 4, has_report: 1 });
  assert.deepStrictEqual(v.rows, [{ label: 'Exported now', value: 0 }, { label: 'Failed', value: 2 },
                                  { label: 'Bad', value: 0 }]);
  assert.strictEqual(v.more, 'and 4 more in the report');
  assert.strictEqual(v.hasReport, true);
  assert.strictEqual(v.tone, 'finished');
  assert.strictEqual(s.normalise({ headline: 'Stopped: The disk is full.' }).tone, 'stopped');
  assert.strictEqual(s.normalise({ headline: 'Cancelled' }).tone, 'cancelled');
  assert.strictEqual(s.normalise(null).headline, 'Finished');
  assert.strictEqual(s.normalise({ more: 0 }).more, '');
  const many = s.normalise({ failures: [1, 2, 3, 4, 5].map(function (n) { return { name: 'D' + n, reason: 'r' }; }) });
  assert.strictEqual(many.failures.length, s.FAILURES_SHOWN);
  assert.strictEqual(s.rowClass('Already up to date'), 'already-up-to-date');
  assert.strictEqual(s.rowClass('Failed'), 'failed');
}
console.log('summary logic ok');

// Escape closes; Enter closes unless a button has the focus.
assert.strictEqual(s.keyAction('Escape', true), 'close');
assert.strictEqual(s.keyAction('Enter', false), 'close');
assert.strictEqual(s.keyAction('Enter', true), null);
assert.strictEqual(s.keyAction('a', false), null);
console.log('summary keys ok');

// Page wiring with a minimal stand-in for the browser and Fusion's bridge.
{
  function El(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.className = ''; this._text = '';
  }
  El.prototype.appendChild = function (c) { this.children.push(c); return c; };
  El.prototype.append = function () { for (let i = 0; i < arguments.length; i++) this.appendChild(arguments[i]); };
  El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  El.prototype.focus = function () { doc.activeElement = this; };
  Object.defineProperty(El.prototype, 'innerHTML', { set: function () { throw new Error('innerHTML used'); } });
  Object.defineProperty(El.prototype, 'textContent', {
    get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); },
    set: function (v) { this._text = String(v); this.children = []; } });
  El.prototype.all = function (out) {
    out = out || [];
    this.children.forEach(function (c) { out.push(c); c.all(out); });
    return out;
  };
  const page = new El('div');
  const listeners = {};
  const doc = { createElement: function (t) { return new El(t); }, getElementById: function () { return page; },
                addEventListener: function (k, f) { listeners[k] = f; } };
  const requests = [];
  const evil = '<img src=x onerror=alert(1)> & "Part"';
  const bridge = { fusionSendData: function (action, data) {
    requests.push([action, JSON.parse(data)]);
    if (action === 'summary') {
      return Promise.resolve(JSON.stringify({ headline: 'Finished', rows: [['Exported now', 3], ['Failed', 1]],
        failures: [{ name: evil, reason: 'Synthetic <b>failure</b>' }], more: 2, retry: 'Run again to retry the 1 failed.',
        notes: ['Low disk space: 200 MB free.'], output: '/Users/you/<Export>', has_report: true }));
    }
    if (action === 'open' && JSON.parse(data).what === 'folder') return JSON.stringify({ error: 'Could not open it.' });
    return JSON.stringify({ ok: true });
  } };
  const win = { addEventListener: function (k, f) { this.onload = f; } };
  const context = { document: doc, window: win, adsk: bridge, setTimeout: function () {}, Date: Date, Promise: Promise,
                    JSON: JSON, String: String, Array: Array, Math: Math, Number: Number, Error: Error };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'FusionBulkExport', 'ui', 'summary.js'), 'utf8'), context);
  win.onload();
  const settle = function () { return new Promise(function (r) { setImmediate(r); }); };
  (async function () {
    await settle(); await settle();
    const text = page.textContent;
    assert.ok(text.indexOf(evil + ':') >= 0, text);  // shown as text, not markup
    assert.ok(text.indexOf('Synthetic <b>failure</b>') >= 0);
    assert.ok(text.indexOf('and 2 more in the report') >= 0);
    assert.ok(text.indexOf('Run again to retry the 1 failed.') >= 0);
    assert.ok(text.indexOf('/Users/you/<Export>') >= 0);
    assert.ok(page.all().every(function (e) { return e.tagName !== 'IMG'; }));
    const name = page.all().filter(function (e) { return e.className === 'name'; })[0];
    assert.strictEqual(name.title, evil);  // the whole name as tooltip
    const buttons = page.all().filter(function (e) { return e.tagName === 'BUTTON'; });
    assert.deepStrictEqual(buttons.map(function (b) { return b.textContent; }), ['Open report', 'Open folder']);
    assert.strictEqual(doc.activeElement, page);
    assert.deepStrictEqual(requests.map(function (r) { return r[0]; }), ['summary', 'shown']);
    buttons[0].onclick();
    assert.deepStrictEqual(requests[requests.length - 1], ['open', { what: 'report' }]);
    buttons[1].onclick();
    await settle();
    assert.ok(page.textContent.indexOf('Could not open it.') >= 0);
    listeners.keydown({ key: 'Enter', target: buttons[0], preventDefault: function () {} });
    assert.notStrictEqual(requests[requests.length - 1][0], 'close');
    listeners.keydown({ key: 'Escape', target: page, preventDefault: function () {} });
    assert.strictEqual(requests[requests.length - 1][0], 'close');
    console.log('summary page ok');
  })().catch(function (error) { console.error(error); process.exit(1); });
}
