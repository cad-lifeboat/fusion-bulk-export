// The results page shown when an export from the button ends. Pure logic first (tested with
// node), then the page wiring, which only runs inside Fusion's dialog. Every name and path is
// set with textContent, never as HTML.
'use strict';

const BRIDGE_TIMEOUT_MS = 5000;  // how long to wait for Fusion's page bridge before saying so
const FAILURES_SHOWN = 3;

// The add-in's summary data (report.summary()), made safe to show whatever arrives: strings,
// whole numbers, lists. The first row ("Exported now") always shows; other zero rows do not.
function normalise(data) {
  data = data && typeof data === 'object' ? data : {};
  const text = function (v) { return v === undefined || v === null ? '' : String(v); };
  const rows = (Array.isArray(data.rows) ? data.rows : []).filter(function (row, index) {
    return Array.isArray(row) && row.length === 2 && (index === 0 || Number(row[1]) !== 0);
  }).map(function (row) { return { label: text(row[0]), value: Math.max(0, Math.floor(Number(row[1]) || 0)) }; });
  const failures = (Array.isArray(data.failures) ? data.failures : []).slice(0, FAILURES_SHOWN).map(function (f) {
    return { name: text(f && f.name), reason: text(f && f.reason) };
  });
  const more = Math.max(0, Math.floor(Number(data.more) || 0));
  const headline = text(data.headline) || 'Finished';
  return {
    headline: headline,
    tone: headline.indexOf('Stopped') === 0 ? 'stopped' : headline.indexOf('Cancelled') === 0 ? 'cancelled' : 'finished',
    rows: rows,
    failures: failures,
    more: more ? 'and ' + more + ' more in the report' : '',
    retry: text(data.retry),
    notes: (Array.isArray(data.notes) ? data.notes : []).map(text),
    output: text(data.output),
    hasReport: !!data.has_report
  };
}

// A CSS class for a count row from its label: "Failed" -> "failed".
function rowClass(label) {
  return String(label).toLowerCase().replace(/[^a-z]+/g, '-').replace(/^-|-$/g, '');
}

// What a key does on the page: Escape closes; Enter closes unless a button has the focus
// (then the button itself is pressed).
function keyAction(key, onButton) {
  if (key === 'Escape') return 'close';
  if (key === 'Enter' && !onButton) return 'close';
  return null;
}

if (typeof module !== 'undefined') {
  module.exports = { BRIDGE_TIMEOUT_MS: BRIDGE_TIMEOUT_MS, FAILURES_SHOWN: FAILURES_SHOWN, normalise: normalise,
                     rowClass: rowClass, keyAction: keyAction };
}

// ---- Page wiring (inside Fusion only) ----
if (typeof document !== 'undefined') {
  function ask(action, data) {
    // Fusion returns the add-in's reply as a Promise (newer builds) or a string.
    const reply = adsk.fusionSendData(action, JSON.stringify(data || {}));
    return Promise.resolve(reply).then(function (text) { return text ? JSON.parse(text) : null; });
  }

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  let statusLine = null;

  function status(text) {
    if (statusLine) statusLine.textContent = text || '';
  }

  function open(what) {
    status('');
    ask('open', { what: what }).then(function (reply) {
      if (reply && reply.error) status(reply.error);
    }, function (error) { status(String(error && error.message || error)); });
  }

  function button(label, action) {
    const b = el('button', '', label);
    b.type = 'button';
    b.onclick = action;
    return b;
  }

  function render(data) {
    const v = normalise(data);
    const page = document.getElementById('page');
    page.textContent = '';
    page.appendChild(el('h1', v.tone, v.headline));
    const counts = el('div', 'counts');
    v.rows.forEach(function (row) {
      const line = el('div', 'count ' + rowClass(row.label));
      line.title = row.label;
      line.append(el('span', 'label', row.label), el('span', 'value', String(row.value)));
      counts.appendChild(line);
    });
    page.appendChild(counts);
    if (v.failures.length) {
      const list = el('ul', 'failures');
      v.failures.forEach(function (f) {
        const li = el('li', 'failure');
        const name = el('span', 'name', f.name + ':');
        name.title = f.name;  // the whole name, when it ends in an ellipsis
        li.append(name, el('span', 'reason', f.reason));
        list.appendChild(li);
      });
      page.appendChild(list);
      if (v.more) page.appendChild(el('div', 'more', v.more));
    }
    if (v.retry) page.appendChild(el('div', 'retry', v.retry));
    if (v.notes.length) {
      const notes = el('ul', 'notes');
      v.notes.forEach(function (n) { notes.appendChild(el('li', '', n)); });
      page.appendChild(notes);
    }
    page.appendChild(el('div', 'folder-label', 'Saved in'));
    page.appendChild(el('div', 'folder', v.output));
    const actions = el('div', 'actions');
    if (v.hasReport) actions.appendChild(button('Open report', function () { open('report'); }));
    actions.appendChild(button('Open folder', function () { open('folder'); }));
    page.appendChild(actions);
    statusLine = el('div', 'status');
    statusLine.setAttribute('role', 'status');
    page.appendChild(statusLine);
    page.focus();  // Enter and Escape close the dialog from here
  }

  function message(text) {
    const page = document.getElementById('page');
    page.textContent = '';
    page.appendChild(el('div', 'waiting', text));
  }

  // Messages from the add-in (BrowserCommandInput.sendInfoToHTML): none are sent, but the page
  // answers like the tree page does.
  window.fusionJavaScriptHandler = { handle: function () { return 'OK'; } };
  window.addEventListener('load', function () {
    document.addEventListener('keydown', function (e) {
      const target = e.target || {};
      if (keyAction(e.key, String(target.tagName || '').toUpperCase() === 'BUTTON') === 'close') {
        e.preventDefault();
        ask('close').then(function (reply) { if (reply && reply.error) status(reply.error); }, function () {});
      }
    });
    const started = Date.now();
    (function start() {
      if (typeof adsk === 'undefined' || !adsk.fusionSendData) {
        if (Date.now() - started < BRIDGE_TIMEOUT_MS) setTimeout(start, 100);
        else message('Fusion did not send the results. Close this dialog to see them in a message.');
        return;
      }
      ask('summary').then(function (reply) {
        if (!reply || reply.error) throw new Error((reply && reply.error) || 'No answer from Fusion.');
        render(reply);
        ask('shown');  // without this, closing the dialog shows the results in a message instead
      }).catch(function (error) {
        message('Could not show the results (' + String(error && error.message || error) + '). ' +
                'Close this dialog to see them in a message.');
      });
    })();
  });
}
