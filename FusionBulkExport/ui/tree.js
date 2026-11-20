// Tree state for the "choose what to export" browser: hubs > projects > folders,
// tri-state checkboxes, folders loaded on demand. Pure logic first (tested with node),
// then the page wiring, which only runs inside Fusion's dialog.
'use strict';

const OFF = 0, ON = 1, SOME = 2;
const BRIDGE_TIMEOUT_MS = 5000;  // how long to wait for Fusion's page bridge before saying so

// own: whether the files directly inside this project or folder are included. A path ending
// in '' in the selection means "only the files directly in this folder".
function node(kind, name, extra) {
  return Object.assign({ kind: kind, name: name, state: OFF, own: false, open: false, children: null,
                         parent: null, loading: false, error: null, files: null, exported: 0 }, extra);
}

// hubs: [{id, name, projects: [{id, name, exported}]}]
function build(hubs) {
  return hubs.map(function (hub) {
    const h = node('hub', hub.name, { id: hub.id });
    h.children = hub.projects.map(function (p) {
      const n = node('project', p.name, { id: p.id, project: p.id, path: [], ids: [], exported: p.exported || 0 });
      n.parent = h;
      return n;
    });
    return h;
  });
}

// Subfolders below a project or folder: names, or {id, name} (folders are then addressed by
// id, so two siblings with the same name stay apart). Children inherit a full tick.
function addFolders(parent, folders) {
  parent.children = folders.map(function (f) {
    const name = typeof f === 'string' ? f : f.name;
    const id = typeof f === 'string' ? null : (f.id === undefined ? null : f.id);
    const n = node('folder', name, { project: parent.project, path: parent.path.concat([name]),
                                     ids: parent.ids.concat([id]), id: id });
    n.parent = parent;
    n.state = parent.state === ON ? ON : OFF;
    n.own = n.state === ON;
    return n;
  });
}

// Opening a project or folder whose subfolders are not known yet: a loading row shows.
function startLoading(n) {
  n.open = true;
  n.loading = true;
  n.error = null;
}

// The add-in's answer to a "folders" request: {folders: [...], files: N} or {error: "..."}.
// Anything else (no answer, a failed request) is an error too, never an empty folder.
function applyFolders(n, reply) {
  n.loading = false;
  if (reply && Array.isArray(reply.folders)) {
    addFolders(n, reply.folders);
    n.files = typeof reply.files === 'number' ? reply.files : null;
    n.error = null;
  } else {
    n.children = null;  // unknown: Retry asks again
    n.error = (reply && reply.error) ? String(reply.error) : 'No answer from Fusion.';
  }
  return n;
}

function setDown(n, state) {
  n.state = state;
  n.own = state === ON;
  (n.children || []).forEach(function (c) { setDown(c, state); });
}

function refreshUp(n) {
  for (let p = n.parent; p; p = p.parent) {
    const states = p.children.map(function (c) { return c.state; });
    // Hubs hold no files of their own; projects and folders do.
    const own = p.kind === 'hub' ? true : p.own;
    const none = p.kind === 'hub' ? true : !p.own;
    p.state = own && states.every(function (s) { return s === ON; }) ? ON
      : none && states.every(function (s) { return s === OFF; }) ? OFF : SOME;
    if (p.kind === 'hub') p.own = p.state === ON;
  }
}

// Clicking a box: a partly ticked or unticked box becomes ticked, a ticked one unticked.
function toggle(n) {
  setDown(n, n.state === ON ? OFF : ON);
  refreshUp(n);
}

// A folder's path for the add-in: per folder {id, name} when its id is known (exactly that
// folder, even beside a same-named sibling), else its name.
function address(n) {
  return n.path.map(function (name, i) {
    const id = n.ids[i];
    return id === null || id === undefined ? name : { id: id, name: name };
  });
}

// {project id: null (whole project) | [folder paths]} — the add-in's Selection format.
function selection(roots) {
  const chosen = {};
  function folders(n, out) {
    if (n.own) out.push(address(n).concat(['']));
    (n.children || []).forEach(function (c) {
      if (c.state === ON) out.push(address(c));
      else if (c.state === SOME) folders(c, out);
    });
    return out;
  }
  roots.forEach(function (hub) {
    hub.children.forEach(function (p) {
      if (p.state === ON) chosen[p.id] = null;
      else if (p.state === SOME) chosen[p.id] = folders(p, []);
    });
  });
  return chosen;
}

function matches(name, filter) {
  const text = (filter || '').trim().toLowerCase();
  return !text || String(name).toLowerCase().indexOf(text) >= 0;
}

// The rows on screen, in order: [{node, depth}]. The filter applies to project names; a hub
// shows while any of its projects does.
function visibleRows(roots, filter) {
  const rows = [];
  function walk(n, depth) {
    rows.push({ node: n, depth: depth });
    if (n.open && n.children) n.children.forEach(function (c) { walk(c, depth + 1); });
  }
  roots.forEach(function (hub) {
    const projects = hub.children.filter(function (p) { return matches(p.name, filter); });
    if (!projects.length) return;
    rows.push({ node: hub, depth: 0 });
    if (hub.open) projects.forEach(function (p) { walk(p, 1); });
  });
  return rows;
}

function expandable(n) {
  return !(n.kind === 'folder' && n.children && n.children.length === 0);
}

// Keyboard, as in a file browser: {focus: row index, action: null | 'toggle' | 'open' | 'close'}.
// Up/Down move; Right opens (or moves to the first child); Left closes (or moves to the parent);
// Space ticks; Enter opens or closes; Home/End go to the first/last row.
function keyAction(rows, index, key) {
  const last = rows.length - 1;
  const here = rows[index];
  if (!here) return { focus: 0, action: null };
  const n = here.node;
  switch (key) {
    case 'ArrowDown': return { focus: Math.min(index + 1, last), action: null };
    case 'ArrowUp': return { focus: Math.max(index - 1, 0), action: null };
    case 'Home': return { focus: 0, action: null };
    case 'End': return { focus: last, action: null };
    case ' ': case 'Spacebar': return { focus: index, action: 'toggle' };
    case 'Enter': return { focus: index, action: expandable(n) ? (n.open ? 'close' : 'open') : null };
    case 'ArrowRight':
      if (!expandable(n)) return { focus: index, action: null };
      if (!n.open) return { focus: index, action: 'open' };
      return { focus: rows[index + 1] && rows[index + 1].node.parent === n ? index + 1 : index, action: null };
    case 'ArrowLeft':
      if (n.open && expandable(n)) return { focus: index, action: 'close' };
      for (let i = index - 1; i >= 0; i--) if (rows[i].node === n.parent) return { focus: i, action: null };
      return { focus: index, action: null };
    default: return { focus: index, action: null };
  }
}

// {project id: items exported there} from the output folder: marks projects in the tree.
function markExported(roots, counts) {
  roots.forEach(function (hub) {
    hub.children.forEach(function (p) { p.exported = (counts && counts[p.id]) || 0; });
  });
}

if (typeof module !== 'undefined') {
  module.exports = { OFF: OFF, ON: ON, SOME: SOME, BRIDGE_TIMEOUT_MS: BRIDGE_TIMEOUT_MS, build: build,
                     addFolders: addFolders, startLoading: startLoading, applyFolders: applyFolders,
                     toggle: toggle, selection: selection, address: address, matches: matches, visibleRows: visibleRows,
                     keyAction: keyAction, markExported: markExported };
}

// ---- Page wiring (inside Fusion only) ----
if (typeof document !== 'undefined') {
  let roots = [];
  let filter = '';
  let focused = null;  // the node whose row takes Tab focus
  let hubsFailed = null;

  function ask(action, data) {
    // Fusion returns the add-in's reply as a Promise (newer builds) or a string.
    const reply = adsk.fusionSendData(action, JSON.stringify(data || {}));
    return Promise.resolve(reply).then(function (text) { return text ? JSON.parse(text) : null; });
  }

  function report() {
    ask('selection', selection(roots));
  }

  function message(text, retry) {
    const ul = document.getElementById('tree');
    ul.textContent = '';
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = text;
    if (retry) li.appendChild(retryButton(retry));
    ul.appendChild(li);
  }

  function retryButton(action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'retry';
    button.textContent = 'Retry';
    button.onclick = function (e) { e.stopPropagation(); action(); };
    return button;
  }

  function note(text, depth, kind, retry) {
    const li = document.createElement('li');
    li.className = kind;
    li.setAttribute('role', 'none');
    li.style.paddingLeft = (depth * 16 + 24) + 'px';
    li.textContent = text;
    if (retry) li.appendChild(retryButton(retry));
    return li;
  }

  function row(n, depth, rows) {
    const li = document.createElement('li');
    li.setAttribute('role', 'treeitem');
    li.setAttribute('aria-level', String(depth + 1));
    li.setAttribute('aria-checked', n.state === ON ? 'true' : n.state === SOME ? 'mixed' : 'false');
    li.setAttribute('aria-selected', n === focused ? 'true' : 'false');
    const open = expandable(n);
    if (open) li.setAttribute('aria-expanded', n.open ? 'true' : 'false');
    li.tabIndex = n === focused ? 0 : -1;
    li.nodeRef = n;
    const line = document.createElement('div');
    line.className = 'row';
    line.style.paddingLeft = (depth * 16 + 4) + 'px';
    const caret = document.createElement('span');
    caret.className = 'caret' + (open ? '' : ' leaf');
    caret.textContent = open ? (n.open ? '▾' : '▸') : '';
    const box = document.createElement('span');
    box.className = 'box ' + ['off', 'on', 'some'][n.state];
    const icon = document.createElement('span');
    icon.className = 'icon ' + n.kind;
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = n.name;
    line.append(caret, box, icon, label);
    if (n.open && n.files !== null && n.kind !== 'hub') {
      const count = document.createElement('span');
      count.className = 'count';
      count.textContent = n.files === 1 ? '1 file' : n.files + ' files';
      line.appendChild(count);
    }
    if (n.kind === 'project' && n.exported) {
      const done = document.createElement('span');
      done.className = 'badge';
      done.textContent = n.exported + ' exported';
      done.title = 'Already in the chosen output folder';
      line.appendChild(done);
    }
    // The box ticks; the rest of the row opens and closes, as in a file browser.
    box.onclick = function (e) { e.stopPropagation(); focused = n; tick(n); };
    line.onclick = function () { focused = n; if (open) expand(n); else render(); };
    li.appendChild(line);
    if (n.open && n.kind !== 'hub') {
      const ul = document.createElement('ul');
      ul.setAttribute('role', 'group');
      if (n.loading) ul.appendChild(note('Loading…', depth + 1, 'loading'));
      else if (n.error) ul.appendChild(note('Could not list the folders: ' + n.error + ' ', depth + 1, 'error',
                                            function () { load(n); }));
      else if (n.children && n.children.length === 0) ul.appendChild(note('No folders', depth + 1, 'empty'));
      (n.loading || n.error ? [] : n.children || []).forEach(function (c) { ul.appendChild(row(c, depth + 1, rows)); });
      li.appendChild(ul);
    } else if (n.open && n.kind === 'hub') {
      const ul = document.createElement('ul');
      ul.setAttribute('role', 'group');
      rows.filter(function (r) { return r.node.parent === n; })
        .forEach(function (r) { ul.appendChild(row(r.node, depth + 1, rows)); });
      li.appendChild(ul);
    }
    return li;
  }

  function render(keepFocus) {
    const ul = document.getElementById('tree');
    const rows = visibleRows(roots, filter);
    if (!rows.some(function (r) { return r.node === focused; })) focused = rows.length ? rows[0].node : null;
    ul.textContent = '';
    rows.filter(function (r) { return r.depth === 0; }).forEach(function (r) { ul.appendChild(row(r.node, 0, rows)); });
    if (!rows.length) message(filter ? 'No project name contains "' + filter + '".' : 'No projects.');
    if (keepFocus && focused) {
      const items = ul.querySelectorAll('[role="treeitem"]');
      for (let i = 0; i < items.length; i++) if (items[i].nodeRef === focused) items[i].focus();
    }
  }

  function tick(n) {
    toggle(n);
    render(true);
    report();
  }

  function load(n) {
    startLoading(n);
    render(true);
    ask('folders', { project: n.project, path: n.path, ids: n.ids }).then(function (reply) {
      applyFolders(n, reply);
      render(true);
    }, function (error) {
      applyFolders(n, { error: String(error) });
      render(true);
    });
  }

  function expand(n) {
    if (n.open) { n.open = false; render(true); return; }
    if (n.kind !== 'hub' && (n.children === null || n.error)) { load(n); return; }
    n.open = true;
    render(true);
  }

  function onKey(e) {
    const rows = visibleRows(roots, filter);
    const index = rows.findIndex(function (r) { return r.node === focused; });
    if (index < 0) return;
    const move = keyAction(rows, index, e.key);
    if (move.action === null && move.focus === index && ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home',
        'End', ' ', 'Enter'].indexOf(e.key) < 0) return;
    e.preventDefault();
    focused = rows[move.focus].node;
    if (move.action === 'toggle') tick(focused);
    else if (move.action === 'open' || move.action === 'close') expand(focused);
    else render(true);
  }

  function loadHubs() {
    message('Loading your hubs…');
    ask('hubs').then(function (reply) {
      if (!reply || !Array.isArray(reply.hubs)) throw new Error((reply && reply.error) || 'No answer from Fusion.');
      if (!reply.hubs.length) { message('Fusion shows no hubs. Sign in to Fusion, then close and reopen this dialog.'); return; }
      roots = build(reply.hubs);
      roots.forEach(function (h) { h.open = true; });
      render();
    }).catch(function (error) {
      message('Could not list your hubs: ' + String(error && error.message || error) + ' ', loadHubs);
    });
  }

  // Messages from the add-in (BrowserCommandInput.sendInfoToHTML).
  window.fusionJavaScriptHandler = { handle: function (action, data) {
    try {
      if (action === 'exported') { markExported(roots, JSON.parse(data || '{}')); render(); }
    } catch (error) { /* a bad message must not break the page */ }
    return 'OK';
  } };
  window.addEventListener('load', function () {
    document.getElementById('tree').addEventListener('keydown', onKey);
    document.getElementById('filter').addEventListener('input', function (e) { filter = e.target.value; render(); });
    // The page can load before Fusion's bridge is ready; wait a little, then say so.
    const started = Date.now();
    (function start() {
      if (typeof adsk === 'undefined' || !adsk.fusionSendData) {
        if (Date.now() - started < BRIDGE_TIMEOUT_MS) setTimeout(start, 100);
        else message('Fusion did not connect to this list. Close the dialog and open it again; ' +
                     'if this keeps happening, choose Everything instead.');
        return;
      }
      loadHubs();
    })();
  });
}
