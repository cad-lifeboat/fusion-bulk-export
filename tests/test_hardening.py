"""Regression tests: files that must never be overwritten, documents that must never be closed,
and runs that must not stop or loop for the wrong reason."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

from tests import fake_adsk as fake
from fusion_bulk_export import names
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export import fusion  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)

    def run_export(self, data, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        return exporter, counts

    def paths(self):
        return {e['id']: [f['path'] for f in e['files']] + ([e['thumbnail']['path']] if e.get('thumbnail') else [])
                for e in self.manifest['entries']}


class SharedNamesTests(Base):
    def uploads_and_design(self):
        upload = fake.File('Bracket v3', 'upload', extension='step')
        upload.content = b'USER UPLOADED STEP'
        render = fake.File('Bracket v3', 'render', extension='png')
        render.content = b'USER RENDER PNG'
        design = fake.File('Bracket', 'bracket')  # version 3: "Bracket v3.step", "Bracket v3.png"
        return upload, render, design

    def test_uploads_listed_before_a_design_keep_their_files(self):
        upload, render, design = self.uploads_and_design()
        data = fake.data([upload, render, design])
        _, counts = self.run_export(data)
        self.assertEqual((counts['exported'], counts['files']), (1, 2))
        paths = self.paths()
        self.assertEqual(paths['upload'], ['Hub/Project/Bracket v3.step'])
        self.assertEqual(paths['render'], ['Hub/Project/Bracket v3.png'])
        moved = 'Bracket~%s v3' % names.tag('file:bracket')
        self.assertEqual(paths['bracket'], ['Hub/Project/%s.f3d' % moved, 'Hub/Project/%s.step' % moved,
                                            'Hub/Project/%s.png' % moved])
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.step').read_bytes(), b'USER UPLOADED STEP')
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.png').read_bytes(), b'USER RENDER PNG')
        for _ in range(2):  # stable: nothing is exported or saved again
            _, counts = self.run_export(data)
            self.assertEqual(counts['exported'] + counts['files'] + counts['failed'], 0)
            self.assertEqual(self.paths(), paths)

    def test_design_listed_first_keeps_its_names_and_uploads_move_aside(self):
        upload, render, design = self.uploads_and_design()
        data = fake.data([design, upload, render])
        self.run_export(data)
        paths = self.paths()
        self.assertEqual(paths['bracket'], ['Hub/Project/Bracket v3.f3d', 'Hub/Project/Bracket v3.step',
                                            'Hub/Project/Bracket v3.png'])
        self.assertEqual(paths['render'], ['Hub/Project/Bracket v3~%s.png' % names.tag('file:render')])
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.png').read_bytes(), fake.PNG)
        _, counts = self.run_export(data)
        self.assertEqual(counts['exported'] + counts['files'], 0)

    def test_a_file_nobody_recorded_is_never_written_over(self):
        folder = self.root / 'Hub' / 'Project'
        folder.mkdir(parents=True)
        (folder / 'Bracket v3.png').write_bytes(b'mine')
        (folder / 'Spec.pdf').write_bytes(b'mine too')
        self.run_export(fake.data([fake.File('Bracket', 'bracket'), fake.File('Spec.pdf', 'spec', extension='pdf')]))
        self.assertEqual((folder / 'Bracket v3.png').read_bytes(), b'mine')
        self.assertEqual((folder / 'Spec.pdf').read_bytes(), b'mine too')
        self.assertEqual(self.paths()['spec'], ['Hub/Project/Spec~%s.pdf' % names.tag('file:spec')])
        self.assertTrue(self.paths()['bracket'][0].startswith('Hub/Project/Bracket~'))

    def test_a_folder_and_a_file_of_the_same_name_stay_apart(self):
        notes = fake.File('Notes', 'notes', extension='')
        part = fake.File('Part', 'part')
        self.run_export(fake.data([notes], [fake.folder('Notes', files=[part])]))
        paths = self.paths()
        self.assertEqual(paths['notes'], ['Hub/Project/Notes'])
        self.assertTrue(paths['part'][0].startswith('Hub/Project/Notes~'), paths)

    def test_a_second_attempt_writes_the_same_names(self):
        design = fake.File('Bracket', 'bracket')
        design.failures = 1
        self.run_export(fake.data([design]))
        self.assertEqual(self.paths()['bracket'][0], 'Hub/Project/Bracket v3.f3d')

    def test_a_failed_thumbnail_leaves_the_file_at_its_path(self):
        target = self.root / 'picture.png'
        target.write_bytes(b'already here')
        staging = fusion.Staging(self.root)

        def half(path):
            Path(path).write_bytes(b'half')
            raise RuntimeError('Synthetic thumbnail failure')
        for future in (NS(state=1, dataObject=NS(saveToFile=lambda path: False)),
                       NS(state=1, dataObject=NS(saveToFile=half))):
            self.assertIsNone(fusion.save_thumbnail(future, str(target), staging))
            self.assertEqual(target.read_bytes(), b'already here')
        self.assertEqual(list(staging.folder.glob('*')), [])


if __name__ == '__main__':
    unittest.main()
