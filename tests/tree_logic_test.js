// Tree logic of FusionBulkExport/ui/tree.js; run with node (tests/test_tree_js.py does).
const assert = require('assert');
const t = require('../FusionBulkExport/ui/tree.js');

const roots = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p1', name: 'P1' }, { id: 'p2', name: 'P2' }] }]);
const [p1, p2] = roots[0].children;
assert.deepStrictEqual(t.selection(roots), {});

// Folders load on demand; ticking one makes its project partial and selects just that path.
t.addFolders(p1, ['Top', 'Other']);
const [top, other] = p1.children;
t.addFolders(top, ['Sub']);
t.toggle(top);
assert.strictEqual(p1.state, t.SOME);
assert.strictEqual(roots[0].state, t.SOME);
assert.strictEqual(top.children[0].state, t.ON);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top']] });

// Nested folder only: the path goes deep, and Top stays partial (its own files are not ticked).
t.toggle(top);
t.toggle(top.children[0]);
assert.strictEqual(top.state, t.SOME);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top', 'Sub']] });

// Every folder ticked still leaves the project's own files out: it counts as whole only when ticked.
t.toggle(other);
assert.strictEqual(p1.state, t.SOME);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top', 'Sub'], ['Other']] });
t.toggle(p1);
assert.deepStrictEqual(t.selection(roots), { p1: null });

// A partly ticked hub becomes fully ticked when clicked; clicking again clears everything.
assert.strictEqual(roots[0].state, t.SOME);
t.toggle(roots[0]);
assert.strictEqual(p2.state, t.ON);
assert.deepStrictEqual(t.selection(roots), { p1: null, p2: null });
t.toggle(roots[0]);
assert.deepStrictEqual(t.selection(roots), {});

// Folders opened under a ticked project arrive ticked.
t.toggle(p2);
t.addFolders(p2, ['A']);
assert.strictEqual(p2.children[0].state, t.ON);
console.log('tree logic ok');

// Own files: ticking every subfolder does not tick the parent's own files.
{
  const r = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P' }] }]);
  const p = r[0].children[0];
  t.addFolders(p, ['Only']);
  t.toggle(p.children[0]);
  assert.strictEqual(p.state, t.SOME);
  assert.deepStrictEqual(t.selection(r), { p: [['Only']] });
  // Unticking one folder of a fully ticked project keeps the project's own files.
  t.toggle(p);           // SOME -> ON (whole)
  t.addFolders(p.children[0], []);
  t.toggle(p.children[0]);
  assert.strictEqual(p.state, t.SOME);
  assert.deepStrictEqual(t.selection(r), { p: [['']] });
}
console.log('own files ok');
