// Logic and page wiring of FusionBulkExport/ui/summary.js; run with node (tests/test_tree_js.py does).
// The summary itself is in the page as HTML (results.py, tested in Python); the script only wires
// the buttons, and must cope with Fusion dropping or never answering a message.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const s = require('../FusionBulkExport/ui/summary.js');

const settle = function () { return new Promise(function (r) { setImmediate(r); }); };

// A timer the test runs by hand: run() fires every callback due so far.
function Timers() {
  const due = [];
  const timer = function (fn, ms) { due.push({ fn: fn, ms: ms }); };
  timer.due = due;
  timer.run = function () { while (due.length) due.shift().fn(); };
  return timer;
}

(async function () {
  // deliver(): every way a click can end, and never a rejection.
  {
    let t = Timers();
    const ok = s.deliver(function () { return Promise.resolve('{"ok":true}'); }, 'open', { what: 'report' }, 2000, t);
    assert.deepStrictEqual(await ok, { ok: true });
    assert.strictEqual(t.due[0].ms, s.CLICK_TIMEOUT_MS);
    t = Timers();
    assert.deepStrictEqual(await s.deliver(function () { return '{"error":"no","path":"/p"}'; }, 'open', {}, 2000, t),
                           { error: 'no', path: '/p' });  // an older Fusion answers with a string
    // The message is lost: the reply never comes; the timer answers.
    t = Timers();
    const lost = s.deliver(function () { return new Promise(function () {}); }, 'open', {}, 2000, t);
    await settle();
    t.run();
    assert.deepStrictEqual(await lost, { timeout: true });
    // An empty reply (the add-in never set returnData) counts as no answer.
    t = Timers();
    const empty = s.deliver(function () { return ''; }, 'open', {}, 2000, t);
    await settle();
    t.run();
    assert.deepStrictEqual(await empty, { timeout: true });
    // No bridge at all.
    t = Timers();
    const none = s.deliver(null, 'open', {}, 2000, t);
    t.run();
    assert.deepStrictEqual(await none, { timeout: true });
    // The bridge throws, or rejects.
    t = Timers();
    assert.deepStrictEqual(await s.deliver(function () { throw new Error('broken'); }, 'open', {}, 2000, t),
                           { error: 'broken' });
    t = Timers();
    assert.deepStrictEqual(await s.deliver(function () { return Promise.reject(new Error('gone')); }, 'open', {}, 2000, t),
                           { error: 'gone' });
    // A reply after the timeout changes nothing.
    t = Timers();
    let late;
    const slow = s.deliver(function () { return new Promise(function (r) { late = r; }); }, 'open', {}, 2000, t);
    t.run();
    late('{"ok":true}');
    assert.deepStrictEqual(await slow, { timeout: true });

    assert.strictEqual(s.clickOutcome({ ok: true }, '/f'), null);
    assert.deepStrictEqual(s.clickOutcome({ timeout: true }, '/f').path, '/f');
    assert.ok(s.clickOutcome({ timeout: true }, '/f').text.indexOf('Fusion did not answer') === 0);
    const failed = s.clickOutcome({ error: 'Could not open it (no viewer).', path: '/r' }, '/f');
    assert.strictEqual(failed.path, '/r');
    assert.ok(failed.text.indexOf('Could not open it (no viewer).') === 0);
    assert.ok(s.CLICK_TIMEOUT_MS <= 2000);
    assert.ok(s.READY_DELAY_MS >= 800);
  }
  console.log('summary logic ok');

  // Escape closes; Enter closes unless a button has the focus.
  assert.strictEqual(s.keyAction('Escape', true), 'close');
  assert.strictEqual(s.keyAction('Enter', false), 'close');
  assert.strictEqual(s.keyAction('Enter', true), null);
  assert.strictEqual(s.keyAction('a', false), null);
  console.log('summary keys ok');

  // Page wiring with a minimal stand-in for the page the add-in writes and Fusion's bridge,
  // which drops the first message (seen on real Fusion) and never answers "folder".
  {
    function El(tag, attrs) {
      this.tagName = tag.toUpperCase(); this.attrs = attrs || {}; this.listeners = {}; this.hidden = false;
      this.textContent = ''; this.value = '';
    }
    El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
    El.prototype.addEventListener = function (k, f) { this.listeners[k] = f; };
    El.prototype.focus = function () { doc.activeElement = this; };
    El.prototype.select = function () { this.selected = true; };
    Object.defineProperty(El.prototype, 'innerHTML', { set: function () { throw new Error('innerHTML used'); } });
    const report = new El('button', { 'data-open': 'report', 'data-path': '/Users/you/<Export>/index.html' });
    const folder = new El('button', { 'data-open': 'folder', 'data-path': '/Users/you/<Export>' });
    const ids = { page: new El('div'), status: new El('div'), manual: new El('div'), 'manual-label': new El('div'),
                  'manual-path': new El('input'), copy: new El('button') };
    ids.manual.hidden = true;
    const listeners = {};
    let copies = 0;
    const doc = { getElementById: function (id) { return ids[id] || null; },
                  querySelectorAll: function (q) { assert.strictEqual(q, 'button[data-open]'); return [report, folder]; },
                  addEventListener: function (k, f) { listeners[k] = f; },
                  execCommand: function (c) { assert.strictEqual(c, 'copy'); copies += 1; return true; } };
    const requests = [];
    let first = true;
    const bridge = { fusionSendData: function (action, data) {
      requests.push([action, JSON.parse(data)]);
      if (first) { first = false; return new Promise(function () {}); }  // dropped
      if (action === 'open' && JSON.parse(data).what === 'folder') return new Promise(function () {});  // no answer
      if (action === 'open') return Promise.resolve(JSON.stringify({ ok: true }));
      return Promise.resolve(JSON.stringify({ ok: true }));
    } };
    const timers = Timers();
    const win = { addEventListener: function (k, f) { this.onload = f; } };
    const context = { document: doc, window: win, adsk: bridge, setTimeout: timers, Promise: Promise, JSON: JSON,
                      String: String, Error: Error };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'FusionBulkExport', 'ui', 'summary.js'), 'utf8'), context);
    win.onload();
    assert.strictEqual(doc.activeElement, ids.page);
    assert.strictEqual(timers.due.length, 1);  // the "ready" ping, later
    assert.ok(timers.due[0].ms >= s.READY_DELAY_MS);
    // The first click is dropped: after 2 s the path shows, to copy.
    report.listeners.click();
    assert.strictEqual(ids.status.textContent, 'Opening…');
    await settle();
    timers.due.filter(function (d) { return d.ms === s.CLICK_TIMEOUT_MS; }).forEach(function (d) { d.fn(); });
    await settle(); await settle();
    assert.strictEqual(ids.manual.hidden, false);
    assert.strictEqual(ids['manual-path'].value, '/Users/you/<Export>/index.html');
    assert.ok(ids['manual-label'].textContent.indexOf('Fusion did not answer') === 0);
    ids.copy.listeners.click();
    assert.strictEqual(copies, 1);
    assert.strictEqual(ids['manual-path'].selected, true);
    assert.strictEqual(ids.status.textContent, 'Copied.');
    // The next click arrives: it opens, and the path box goes away.
    timers.due.length = 0;
    report.listeners.click();
    await settle(); await settle();
    assert.strictEqual(ids.manual.hidden, true);
    assert.strictEqual(ids.status.textContent, '');
    assert.deepStrictEqual(requests.map(function (r) { return r[1].what; }), ['report', 'report']);
    // Open folder never gets an answer: its path shows.
    timers.due.length = 0;
    folder.listeners.click();
    await settle();
    timers.due.filter(function (d) { return d.ms === s.CLICK_TIMEOUT_MS; }).forEach(function (d) { d.fn(); });
    await settle(); await settle();
    assert.strictEqual(ids['manual-path'].value, '/Users/you/<Export>');
    // Keys: Enter on a button presses it; Escape closes.
    listeners.keydown({ key: 'Enter', target: report, preventDefault: function () {} });
    assert.notStrictEqual(requests[requests.length - 1][0], 'close');
    listeners.keydown({ key: 'Escape', target: ids.page, preventDefault: function () {} });
    assert.strictEqual(requests[requests.length - 1][0], 'close');
    console.log('summary page ok');
  }
})().catch(function (error) { console.error(error); process.exit(1); });
