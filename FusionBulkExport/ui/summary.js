// The results page's buttons. The summary itself is already in the page as plain HTML (the
// add-in writes the page for each run, see results.py), so it shows with no script and no
// message from Fusion. This script only wires Open report, Open folder, Enter and Escape to the
// add-in (adsk.fusionSendData, answered through incomingFromHTML). A click that gets no answer
// within CLICK_TIMEOUT_MS shows the path as selectable text with a Copy button, so nobody is
// stuck. Pure logic first (tested with node), then the page wiring. Text is set with
// textContent and value, never as HTML.
'use strict';

const CLICK_TIMEOUT_MS = 2000;  // a click not answered by then shows the path to copy
const READY_DELAY_MS = 800;     // Fusion drops a message sent at once on load (seen on real Fusion)

// What a key does on the page: Escape closes; Enter closes unless a button has the focus
// (then the button itself is pressed).
function keyAction(key, onButton) {
  if (key === 'Escape') return 'close';
  if (key === 'Enter' && !onButton) return 'close';
  return null;
}

// Send action to the add-in and settle within timeoutMs: {ok: true}, {error, path} (the add-in's
// own answer) or {timeout: true} (no answer: the bridge is missing, the message was lost, or the
// reply never came). send is adsk.fusionSendData; it returns the reply as a Promise (newer
// Fusion) or a string. Never rejects.
function deliver(send, action, data, timeoutMs, timer) {
  return new Promise(function (resolve) {
    let done = false;
    const finish = function (value) { if (!done) { done = true; resolve(value); } };
    timer(function () { finish({ timeout: true }); }, timeoutMs);
    let reply;
    try {
      if (typeof send !== 'function') return;  // no bridge: the timer answers
      reply = send(action, JSON.stringify(data || {}));
    } catch (error) {
      finish({ error: String(error && error.message || error) });
      return;
    }
    Promise.resolve(reply).then(function (text) {
      if (text === undefined || text === null || text === '') return;  // no answer: wait for the timer
      let value;
      try { value = typeof text === 'string' ? JSON.parse(text) : text; } catch (e) { value = null; }
      finish(value && typeof value === 'object' ? value : { ok: true });
    }, function (error) { finish({ error: String(error && error.message || error) }); });
  });
}

// What the page says after a click on Open report / Open folder, from deliver()'s result:
// null (it opened) or {text, path} (shown with the path to copy).
function clickOutcome(result, path) {
  if (result && result.ok) return null;
  if (result && result.timeout) {
    return { text: 'Fusion did not answer. Copy the path and open it from your file browser:', path: path };
  }
  const why = String(result && result.error || 'It could not be opened.');
  return { text: why + ' Copy the path and open it from your file browser:', path: (result && result.path) || path };
}

if (typeof module !== 'undefined') {
  module.exports = { CLICK_TIMEOUT_MS: CLICK_TIMEOUT_MS, READY_DELAY_MS: READY_DELAY_MS, keyAction: keyAction,
                     deliver: deliver, clickOutcome: clickOutcome };
}

// ---- Page wiring (inside Fusion only) ----
if (typeof document !== 'undefined') {
  const bridge = function () {
    return typeof adsk !== 'undefined' && adsk && typeof adsk.fusionSendData === 'function'
      ? function (action, data) { return adsk.fusionSendData(action, data); } : null;
  };
  const byId = function (id) { return document.getElementById(id); };
  const status = function (text) { const s = byId('status'); if (s) s.textContent = text || ''; };

  function manual(outcome) {
    const box = byId('manual');
    if (!box) return;
    if (!outcome) { box.hidden = true; return; }
    byId('manual-label').textContent = outcome.text;
    byId('manual-path').value = outcome.path || '';
    box.hidden = false;
  }

  function copy() {
    const field = byId('manual-path');
    let copied = false;
    try { field.focus(); field.select(); copied = !!document.execCommand('copy'); } catch (e) { copied = false; }
    if (!copied && typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(function () { status('Copied.'); },
        function () { status('Selected: press Ctrl+C (Cmd+C on a Mac) to copy.'); });
      return;
    }
    status(copied ? 'Copied.' : 'Selected: press Ctrl+C (Cmd+C on a Mac) to copy.');
  }

  function open(button) {
    const what = button.getAttribute('data-open');
    const path = button.getAttribute('data-path') || '';
    status('Opening…');
    manual(null);
    deliver(bridge(), 'open', { what: what }, CLICK_TIMEOUT_MS, setTimeout).then(function (result) {
      status('');
      manual(clickOutcome(result, path));
    });
  }

  function close() {
    deliver(bridge(), 'close', {}, CLICK_TIMEOUT_MS, setTimeout).then(function (result) {
      if (!result || !result.ok) status('Use the Close button.');
    });
  }

  window.fusionJavaScriptHandler = { handle: function () { return 'OK'; } };
  window.addEventListener('load', function () {
    const buttons = document.querySelectorAll('button[data-open]');
    for (let i = 0; i < buttons.length; i++) {
      const b = buttons[i];
      b.addEventListener('click', function () { open(b); });
    }
    const copyButton = byId('copy');
    if (copyButton) copyButton.addEventListener('click', copy);
    document.addEventListener('keydown', function (e) {
      const target = e.target || {};
      if (keyAction(e.key, String(target.tagName || '').toUpperCase() === 'BUTTON') === 'close') {
        e.preventDefault();
        close();
      }
    });
    const page = byId('page');
    if (page) page.focus();  // Enter and Escape close the dialog from here
    // Only for the add-in's trace: says the page loaded. Nothing waits for it.
    setTimeout(function () { const send = bridge(); if (send) deliver(send, 'ready', {}, CLICK_TIMEOUT_MS, setTimeout); },
               READY_DELAY_MS);
  });
}
