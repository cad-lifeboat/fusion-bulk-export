"""Assemblies with designs inserted from other designs: their .f3d holds only links, so every
export says how many there are (summary, status, check), and the report names them."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import checks, report
from fusion_bulk_export.core import Exporter
from fusion_bulk_export.fusion import FusionAdapter

spec = importlib.util.spec_from_file_location('autorun_launcher_assemblies',
                                              Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

ONE = ('1 assembly uses designs inserted from other designs. Its .f3d holds only links to them; the full '
       'geometry is in its .step, and every inserted design is in this folder too. For a single file with '
       'everything, export the assembly as .f3z from File > Export in Fusion.')
THREE = ('3 assemblies use designs inserted from other designs. Their .f3d holds only links to them; the full '
         'geometry is in their .step, and every inserted design is in this folder too. For a single file with '
         'everything, export the assembly as .f3z from File > Export in Fusion.')


def design(identity, name, version=1, references=None, status='done', step=True, project='Proj', path=()):
    stem = 'Hub/%s/%s%s v%d' % (project, '/'.join(path) + '/' if path else '', name, version)
    files = [dict(path=stem + '.f3d', role='archive', bytes=1, sha256='x')]
    if step:
        files.append(dict(path=stem + '.step', role='step', bytes=1, sha256='x'))
    entry = dict(key=json.dumps(['h', 'p', identity, version]), kind='design', id=identity, name=name,
                 version=version, status=status, hub='Hub', project=project, path=list(path), files=files)
    if references is not None:
        entry['references'] = references
    return entry


def ref(identity, name):
    return dict(id=identity, name=name)


class NoteTests(unittest.TestCase):
    def test_no_assemblies_no_note(self):
        manifest = dict(entries=[design('a', 'Plain', references=[]), design('b', 'Other')])
        self.assertEqual(checks.inserted_designs(manifest), [])
        self.assertIsNone(checks.inserted_note([]))
        self.assertFalse([n for n in report.summary({}, {}, manifest, '/o')['notes'] if 'assembl' in n])

    def test_one_assembly(self):
        manifest = dict(entries=[design('part', 'Screw'),
                                 design('asm', 'Box', references=[ref('part', 'Screw')])])
        self.assertEqual(checks.inserted_note(checks.inserted_designs(manifest)), ONE)
        self.assertIn(ONE, report.summary({}, {}, manifest, '/o')['notes'])

    def test_many_assemblies_each_design_once(self):
        part = design('part', 'Screw')
        entries = [part] + [design('asm%d' % n, 'Box %d' % n, references=[ref('part', 'Screw'), ref('part', 'Screw')])
                            for n in range(3)]
        # An older version of an assembly beside its newest (older versions kept) counts once.
        entries.append(design('asm0', 'Box 0', version=0, references=[ref('part', 'Screw')]))
        # A failed newer version does not hide the exported one; moved older versions are not counted.
        entries.append(design('asm1', 'Box 1', version=9, status='failed', references=[ref('part', 'Screw')]))
        entries.append(design('asm9', 'Gone', status='moved', references=[ref('part', 'Screw')]))
        found = checks.inserted_designs(dict(entries=entries))
        self.assertEqual([item['entry']['name'] for item in found], ['Box 0', 'Box 1', 'Box 2'])
        self.assertEqual([len(item['inserted']) for item in found], [1, 1, 1])
        self.assertEqual(found[0]['entry']['version'], 1)
        self.assertEqual(checks.inserted_note(found), THREE)
        self.assertNotIn('(s)', THREE)

    def test_inserted_designs_not_in_this_export(self):
        manifest = dict(entries=[design('asm', 'Box', references=[ref('gone', 'Lid'), ref('gone2', 'Hinge')])])
        found = checks.inserted_designs(manifest)
        self.assertEqual([d['path'] for d in found[0]['inserted']], [None, None])
        self.assertEqual(checks.inserted_note(found),
                         '1 assembly uses designs inserted from other designs. Its .f3d holds only links to them; '
                         'the full geometry is in its .step, and the inserted designs are in this folder too, '
                         'except 2 designs not in this export. For a single file with everything, export the '
                         'assembly as .f3z from File > Export in Fusion.')

    def test_without_step_the_note_does_not_promise_one(self):
        manifest = dict(entries=[design('part', 'Screw'),
                                 design('asm', 'Box', references=[ref('part', 'Screw')], step=False)])
        note = checks.inserted_note(checks.inserted_designs(manifest))
        self.assertNotIn('.step', note)
        self.assertIn('Its .f3d holds only links to them; every inserted design is in this folder too.', note)

    def test_older_manifest_without_paths_or_roles(self):
        # A folder from an older version: references without "path", files without "role".
        manifest = dict(entries=[
            dict(name='Screw', id='part', version=2, status='done', files=[dict(path='Hub/P/Screw v2.f3d'),
                                                                         dict(path='Hub/P/Screw v2.step')]),
            dict(name='Box', id='asm', version=4, status='done', files=[dict(path='Hub/P/Box v4.f3d'),
                                                                      dict(path='Hub/P/Box v4.step')],
                 references=[dict(id='part', name='Screw')])])
        found = checks.inserted_designs(manifest)
        self.assertEqual(found[0]['inserted'], [dict(id='part', name='Screw', path='Hub/P/Screw v2.f3d')])
        self.assertEqual(checks.inserted_note(found), ONE)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = dict(entries=[
            design('part', 'Screw <m3>', path=['Parts']),
            design('asm', 'Box & lid', references=[ref('part', 'Screw <m3>'), ref('gone', 'Missing hinge')],
                   path=['Assemblies']),
            design('plain', 'Plain')])

    def test_index_html_lists_each_assembly_with_its_inserted_designs(self):
        page, table = report.write(NS(root=self.root, manifest=self.manifest, run_info={}))
        text = page.read_text(encoding='utf-8')
        self.assertIn('<h2>Assemblies with inserted designs</h2>', text)
        section = text[text.index('Assemblies with inserted designs'):text.index('<h2>Designs</h2>')]
        self.assertIn('<b>Box &amp; lid v1</b>', section)
        self.assertIn('<a href="Hub/Proj/Assemblies/">Proj / Assemblies</a>', section)
        self.assertIn('<a href="Hub/Proj/Assemblies/Box%20%26%20lid%20v1.step">STEP</a>', section)
        self.assertIn('<a href="Hub/Proj/Parts/Screw%20%3Cm3%3E%20v1.f3d">Screw &lt;m3&gt;</a>', section)
        self.assertIn('Missing hinge <span class="muted">(not in this export)</span>', section)
        self.assertNotIn('Plain', section)
        self.assertIn('1 assembly with inserted designs', text)
        rows = {row['design']: row for row in csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig')))}
        self.assertEqual(rows['Box & lid']['inserted_designs'],
                         'Hub/Proj/Parts/Screw <m3> v1.f3d; Missing hinge (not in this export)')
        self.assertEqual(rows['Plain']['inserted_designs'], '')

    def test_no_section_without_assemblies(self):
        self.manifest['entries'][1]['references'] = []
        page, _ = report.write(NS(root=self.root, manifest=self.manifest, run_info={}))
        self.assertNotIn('Assemblies with inserted designs', page.read_text(encoding='utf-8'))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def write_manifest(self, entries):
        (self.out / 'manifest.json').write_bytes(json.dumps(dict(entries=entries)).encode('utf-8'))

    def test_status_counts_assemblies_without_names(self):
        self.write_manifest([design('part', 'Secret screw'),
                             design('asm', 'Secret box', references=[ref('part', 'Secret screw')])])
        text = '\n'.join(launcher.summary(self.out))
        self.assertIn('Note: ' + ONE, text)
        self.assertNotIn('Secret', text)

    def test_status_says_nothing_without_assemblies(self):
        self.write_manifest([design('part', 'Screw', references=[])])
        self.assertFalse([line for line in launcher.summary(self.out) if 'assembl' in line])

    def real_files(self, entry):
        """Write an entry's .f3d and .step for check, as bytes (the same on every system)."""
        import hashlib
        import zipfile
        for info in entry['files']:
            path = self.out / info['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            if info['role'] == 'archive':
                with zipfile.ZipFile(str(path), 'w') as archive:
                    archive.writestr('Manifest.dat', 'x')
                    archive.writestr('FusionAssetName[Active]/x', 'x')
            else:
                path.write_bytes(b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('X'));\nENDSEC;\nDATA;\n#1=MANIFOLD_SOLID_BREP"
                                 b"('',#2);\n" + b'/* x */\n' * 400 + b'ENDSEC;\nEND-ISO-10303-21;\n')
            data = path.read_bytes()
            info.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        entry['step_solids'] = 1
        return entry

    def check(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = launcher.main(['check', str(self.out)])
        return code, stream.getvalue()

    def test_check_prints_the_count_as_a_note_only(self):
        part = self.real_files(design('part', 'Secret screw'))
        self.write_manifest([part])
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertNotIn('assembl', text)
        self.write_manifest([part, self.real_files(design('asm', 'Secret box', references=[ref('part', 'Secret screw')]))])
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn('Note: ' + ONE, text)
        self.assertNotIn('PROBLEM', text)
        self.assertNotIn('Secret', text)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))

    def run_export(self, data):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(data)
        return exporter, counts

    def test_every_run_says_so_up_to_date_assemblies_included(self):
        assembly = fake.File('Box', 'asm', references=2)
        part = assembly.childReferences.items[0]  # the other inserted design is not in the account's folders
        data = fake.data([part, assembly])
        exporter, counts = self.run_export(data)
        self.assertEqual(counts['exported'], 2)
        expected = ('1 assembly uses designs inserted from other designs. Its .f3d holds only links to them; the '
                    'full geometry is in its .step, and the inserted designs are in this folder too, except 1 '
                    'design not in this export.')
        self.assertTrue(any(note.startswith(expected)
                            for note in report.summary(counts, exporter.run_info, exporter.manifest, self.root)['notes']))
        exporter, counts = self.run_export(data)
        self.assertEqual((counts['exported'], counts['up_to_date']), (0, 2))
        text = report.summary_text(counts, exporter.run_info, exporter.manifest, self.root)
        self.assertIn(expected, text)
        log = (self.root / 'export.log').read_text(encoding='utf-8')
        lines = [line for line in log.splitlines() if 'assembly with inserted designs' in line]
        self.assertEqual(len(lines), 2)  # one per run
        self.assertTrue(lines[-1].endswith('"Box": 2 inserted designs, 1 not in this export'))
        status = '\n'.join(launcher.summary(self.root))
        self.assertIn('Note: ' + expected, status)
        self.assertNotIn('Box', status)


if __name__ == '__main__':
    unittest.main()
