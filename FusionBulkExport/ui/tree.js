// Tree state for the "choose what to export" browser: hubs > projects > folders,
// tri-state checkboxes, folders loaded on demand. Pure logic first (tested with node),
// then the page wiring, which only runs inside Fusion's dialog.
'use strict';

const OFF = 0, ON = 1, SOME = 2;

// own: whether the files directly inside this project or folder are included. A path ending
// in '' in the selection means "only the files directly in this folder".
function node(kind, name, extra) {
  return Object.assign({ kind: kind, name: name, state: OFF, own: false, open: false, children: null,
                         parent: null }, extra);
}

// hubs: [{id, name, projects: [{id, name}]}]
function build(hubs) {
  return hubs.map(function (hub) {
    const h = node('hub', hub.name, { id: hub.id });
    h.children = hub.projects.map(function (p) {
      const n = node('project', p.name, { id: p.id, project: p.id, path: [] });
      n.parent = h;
      return n;
    });
    return h;
  });
}

// Folder names below a project or folder; children inherit a full tick from their parent.
function addFolders(parent, names) {
  parent.children = names.map(function (name) {
    const n = node('folder', name, { project: parent.project, path: parent.path.concat([name]) });
    n.parent = parent;
    n.state = parent.state === ON ? ON : OFF;
    n.own = n.state === ON;
    return n;
  });
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

// {project id: null (whole project) | [folder paths]} — the add-in's Selection format.
function selection(roots) {
  const chosen = {};
  function folders(n, out) {
    if (n.own) out.push(n.path.concat(['']));
    (n.children || []).forEach(function (c) {
      if (c.state === ON) out.push(c.path);
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

if (typeof module !== 'undefined') {
  module.exports = { OFF: OFF, ON: ON, SOME: SOME, build: build, addFolders: addFolders,
                     toggle: toggle, selection: selection };
}

// ---- Page wiring (inside Fusion only) ----
if (typeof document !== 'undefined') {
  let roots = [];

  function ask(action, data) {
    // Fusion returns the add-in's reply as a Promise (newer builds) or a string.
    const reply = adsk.fusionSendData(action, JSON.stringify(data || {}));
    return Promise.resolve(reply).then(function (text) { return text ? JSON.parse(text) : null; });
  }

  function report() {
    ask('selection', selection(roots));
  }

  function row(n, depth) {
    const li = document.createElement('li');
    const line = document.createElement('div');
    line.className = 'row';
    line.style.paddingLeft = (depth * 16 + 4) + 'px';
    const caret = document.createElement('span');
    const leaf = n.kind === 'folder' && n.children && n.children.length === 0;
    caret.className = 'caret' + (leaf ? ' leaf' : '');
    caret.textContent = leaf ? '' : (n.open ? '▾' : '▸');
    const box = document.createElement('span');
    box.className = 'box ' + ['off', 'on', 'some'][n.state];
    box.setAttribute('role', 'checkbox');
    box.setAttribute('aria-checked', n.state === ON ? 'true' : n.state === SOME ? 'mixed' : 'false');
    const icon = document.createElement('span');
    icon.className = 'icon ' + n.kind;
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = n.name;
    line.append(caret, box, icon, label);
    // The box ticks; the rest of the row opens and closes, as in a file browser.
    box.onclick = function (e) { e.stopPropagation(); toggle(n); render(); report(); };
    line.onclick = function () { if (!leaf) expand(n); };
    li.appendChild(line);
    if (n.open && n.children) {
      const ul = document.createElement('ul');
      if (n.children.length === 0) {
        const empty = document.createElement('li');
        empty.className = 'empty';
        empty.style.paddingLeft = ((depth + 1) * 16 + 24) + 'px';
        empty.textContent = 'No folders';
        ul.appendChild(empty);
      }
      n.children.forEach(function (c) { ul.appendChild(row(c, depth + 1)); });
      li.appendChild(ul);
    }
    return li;
  }

  function render() {
    const ul = document.getElementById('tree');
    ul.textContent = '';
    roots.forEach(function (h) { ul.appendChild(row(h, 0)); });
  }

  function expand(n) {
    n.open = !n.open;
    if (n.open && n.children === null) {
      n.children = [];
      render();
      ask('folders', { project: n.project, path: n.path }).then(function (reply) {
        addFolders(n, reply.folders);
        render();
      });
      return;
    }
    render();
  }

  window.fusionJavaScriptHandler = { handle: function () { return 'OK'; } };
  window.addEventListener('load', function () {
    // The page can load before Fusion's bridge is ready; retry briefly.
    let tries = 0;
    (function start() {
      if (typeof adsk === 'undefined' || !adsk.fusionSendData) {
        if (tries++ < 50) setTimeout(start, 100);
        return;
      }
      ask('hubs').then(function (reply) {
        roots = build(reply.hubs);
        roots.forEach(function (h) { h.open = true; });
        render();
      }).catch(function (error) {
        document.getElementById('tree').innerHTML = '<li class="empty">Could not list your hubs: ' +
          String(error).replace(/</g, '&lt;') + '</li>';
      });
    })();
  });
}
