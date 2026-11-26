"""The .f3z-style bundle built beside an assembly's .f3d (f3z.py) from the .f3d files the export
wrote: its layout, the graph in DesignDescription.json, the links' neutronRole read from the
assembly's Manifest.dat, what is never built, and that the .f3d files are never changed.

The .f3d files here are synthetic: zips with a Manifest.dat laid out as a real one is (strings
as a 32-bit count of UTF-16 code units, then the text; each link record starts with its role GUID,
then the linked design's paths, "LineageId", "LineageUrn", "toDocDisplayName", "URL"...)."""
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock
import zipfile

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import autorun, checks, f3z, report  # noqa: E402
from fusion_bulk_export.core import Exporter  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402
from tests.test_ui import UIBase  # noqa: E402

spec = importlib.util.spec_from_file_location('autorun_launcher_f3z', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

LINEAGE = 'urn:adsk.wipprod:dm.lineage:'
ROLE_PIN = '11111111-2222-4333-8444-555555555555'
ROLE_BLOCK = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


def text(value):
    """One Manifest.dat string: its count of UTF-16 code units, then the text."""
    return struct.pack('<I', len(value)) + value.encode('utf-16-le')


def manifest_dat(links=()):
    """A Manifest.dat with a link record for each (role, lineage URN, version, name)."""
    data = text('.f3d') + b'\x01\x00' + text('Fusion Document') + text('A Fusion Document') + \
        text('0f0f0f0f-1e1e-4d2d-8c3c-4b4b4b4b4b4b') + b'\x02\x00\x00'
    for role, lineage, version, name in links:
        key = lineage.rsplit(':', 1)[-1]
        data += text(role) + b'\x00\x01' + text('/Users/someone/export/Assembly.f3d') + \
            text('/Users/someone/Library/F/_%s.f3d' % name) + text('LineageId') + text(lineage) + \
            text('LineageUrn') + text(lineage) + text('toDocDisplayName') + text(name) + text('URL') + \
            text('urn:adsk.wipprod:fs.file:vf.%s?version=%d' % (key, version)) + text('linkedAssetGUID') + \
            text('12345678-90ab-4cde-8f01-23456789abcd') + text('linkClassification') + text('OriginalDesign')
    return data + text('FusionAssetName') + text('NA_EXPORT')


def f3d_bytes(links=(), tag='x', method=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(zipfile.ZipInfo('Manifest.dat'), manifest_dat(links), compress_type=method)
        archive.writestr('FusionAssetName[Active]/Breps.BlobParts/body.smb', 'geometry ' + tag)
        archive.writestr('Properties.dat', b'\x02\x00\x00\x00{}')
    return stream.getvalue()


def zstd_header(data):
    """The same zip with Manifest.dat marked as Zstandard (method 93), as Fusion writes it."""
    marker = b'Manifest.dat'
    out = bytearray(data)
    for signature, offset in ((b'PK\x03\x04', 8), (b'PK\x01\x02', 10)):
        start = 0
        while True:
            start = out.find(signature, start)
            if start < 0:
                break
            name_at = start + (30 if signature == b'PK\x03\x04' else 46)
            if out[name_at:name_at + len(marker)] == marker:
                out[start + offset:start + offset + 2] = struct.pack('<H', 93)
            start += 4
    return bytes(out)


class Folder:
    """An export folder with designs written as the export writes them (bytes, sha256 recorded)."""
    def __init__(self, root):
        self.root = Path(root)
        self.entries = []

    def design(self, key, name, version=1, references=None, data=None, status='done', out_of_date=False):
        relative = 'Hub/Proj/%s v%d.f3d' % (name, version)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if data is not None else f3d_bytes(tag=key))
        os.utime(str(path), (1700000000, 1700000000))
        blob = path.read_bytes()
        entry = dict(key=json.dumps(['h', 'p', key, version]), kind='design', id=LINEAGE + key, name=name,
                     version=version, status=status, hub='Hub', project='Proj', path=[], date=1700000000,
                     files=[dict(path=relative, role='archive', bytes=len(blob),
                                 sha256=hashlib.sha256(blob).hexdigest(), mtime_ns=os.stat(str(path)).st_mtime_ns)],
                     references=references if references is not None else [], step='off')
        if out_of_date:
            entry['references_out_of_date'] = True
        self.entries.append(entry)
        return entry

    @property
    def manifest(self):
        return dict(schema=2, runs=[], entries=self.entries)

    def save(self):
        (self.root / 'manifest.json').write_bytes(json.dumps(self.manifest).encode('utf-8'))


def ref(key, name, version=1):
    return dict(id=LINEAGE + key, name=name, version=version)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = Folder(self.root)

    def bracket(self, **kwargs):
        """The real sample's shape: an assembly inserting a Pin (twice in Fusion) and a Block."""
        self.pin = self.folder.design('pin', 'Pin')
        self.block = self.folder.design('block', 'Block')
        data = f3d_bytes([(ROLE_PIN, LINEAGE + 'pin', 1, 'Pin'), (ROLE_BLOCK, LINEAGE + 'block', 1, 'Block')], 'asm')
        self.asm = self.folder.design('asm', 'Bracket', data=data,
                                      references=[ref('pin', 'Pin'), ref('pin', 'Pin'), ref('block', 'Block')], **kwargs)
        return self.asm

    def build(self, older='move'):
        manifest = self.folder.manifest
        tally = f3z.Builder(self.root, manifest, older).run()
        self.folder.entries = manifest['entries']
        return tally

    def bundle(self, entry=None):
        entry = entry or self.asm
        return zipfile.ZipFile(str(self.root / entry['f3z']['path']))

    def graph(self, entry=None):
        description = json.loads(self.bundle(entry).read('DesignDescription.json'))
        return description, description['designDescription']['designGraphs'][0]


class LayoutTests(Base):
    def test_layout_names_and_graph(self):
        self.bracket()
        sources = {path: (path.read_bytes(), os.stat(str(path)).st_mtime_ns) for path in self.root.rglob('*.f3d')}
        tally = self.build()
        self.assertEqual((tally['assemblies'], tally['built'], tally['not_built']), (1, 1, 0))
        self.assertEqual(self.asm['f3z']['path'], 'Hub/Proj/Bracket v1.f3z')
        self.assertEqual(self.asm['f3z']['role'], 'f3z_bundle')
        # The files Fusion wrote are untouched: same bytes, same times, nothing else beside them.
        for path, (data, mtime) in sources.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(os.stat(str(path)).st_mtime_ns, mtime)
        self.assertEqual(sorted(p.name for p in (self.root / 'Hub' / 'Proj').iterdir()),
                         ['Block v1.f3d', 'Bracket v1.f3d', 'Bracket v1.f3z', 'Pin v1.f3d'])
        archive = self.bundle()
        names = archive.namelist()
        # As Fusion's own: Manifest.json, one .f3d per distinct design (sorted), DesignDescription.json.
        self.assertEqual(names[0], 'Manifest.json')
        self.assertEqual(names[-1], 'DesignDescription.json')
        self.assertEqual(len(names), 5)  # the Pin inserted twice is there once
        self.assertEqual(names[1:-1], sorted(names[1:-1]))
        self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist()))
        root = json.loads(archive.read('Manifest.json'))['root']
        self.assertEqual(root, f3z.member_name(LINEAGE + 'asm', 1))
        description, graph = self.graph()
        self.assertEqual(description['version'], '0.1')
        self.assertEqual(description['designDescription']['currentVersion'], '1')
        objects = {item['displayName']: item for item in graph['designObjects']}
        self.assertEqual(graph['rootIds'], [objects['Bracket']['id']])
        for name, entry in (('Bracket', self.asm), ('Pin', self.pin), ('Block', self.block)):
            item = objects[name]
            self.assertEqual(item['relativePath'], item['downloadAs'])
            self.assertIn(item['relativePath'], names)
            self.assertEqual(archive.read(item['relativePath']), (self.root / entry['files'][0]['path']).read_bytes())
            self.assertEqual(item['lineage'], entry['id'])
            self.assertEqual(item['about'], 'urn:adsk.wipprod:fs.file:vf.%s?version=1' % entry['id'].rsplit(':', 1)[1])
            self.assertEqual(item['contentType'], 'f3d')
            self.assertEqual(item['metadata']['rootFileName'], name + '.f3d')
        self.assertEqual(objects['Pin']['references'], [])
        self.assertEqual(objects['Bracket']['references'], [dict(
            type='XREF', ids=[objects['Pin']['id'], objects['Block']['id']],
            relationships=[dict(id=objects['Pin']['id'], metadata=dict(neutronRole=ROLE_PIN)),
                           dict(id=objects['Block']['id'], metadata=dict(neutronRole=ROLE_BLOCK))])])
        self.assertTrue(self.asm['f3z']['roles'])
        self.assertEqual(checks.archive_facts(self.root / self.asm['f3z']['path'])[0], [])

    def test_nested_assemblies_and_shared_designs(self):
        screw = self.folder.design('screw', 'Screw')
        hinge = self.folder.design('hinge', 'Hinge', references=[ref('screw', 'Screw')],
                                   data=f3d_bytes([(ROLE_PIN, LINEAGE + 'screw', 1, 'Screw')], 'hinge'))
        box = self.folder.design('box', 'Box', references=[ref('hinge', 'Hinge'), ref('screw', 'Screw')],
                                 data=f3d_bytes([(ROLE_BLOCK, LINEAGE + 'hinge', 1, 'Hinge'),
                                                 ('0c5a1c39-0000-4000-8000-000000000001', LINEAGE + 'screw', 1, 'Screw')],
                                                'box'))
        tally = self.build()
        self.assertEqual((tally['assemblies'], tally['built']), (2, 2))  # the Hinge gets its own too
        _, graph = self.graph(box)
        objects = {item['displayName']: item for item in graph['designObjects']}
        self.assertEqual(sorted(objects), ['Box', 'Hinge', 'Screw'])  # the Screw once
        self.assertEqual(objects['Box']['references'][0]['ids'], [objects['Hinge']['id'], objects['Screw']['id']])
        self.assertEqual(objects['Hinge']['references'][0]['ids'], [objects['Screw']['id']])
        self.assertEqual(objects['Hinge']['references'][0]['relationships'][0]['metadata'], dict(neutronRole=ROLE_PIN))
        self.assertEqual(len(self.bundle(box).namelist()), 5)
        self.assertEqual(hinge['f3z']['path'], 'Hub/Proj/Hinge v1.f3z')
        self.assertIsNone(screw.get('f3z'))

    def test_link_roles_read_as_on_real_fusion(self):
        roles = f3z.link_roles(manifest_dat([(ROLE_PIN, LINEAGE + 'a', 1, 'A'), (ROLE_BLOCK, LINEAGE + 'b', 4, 'B')]))
        self.assertEqual(roles, {LINEAGE + 'a': ROLE_PIN, LINEAGE + 'b': ROLE_BLOCK})
        self.assertEqual(f3z.link_roles(manifest_dat()), {})
        self.assertIn('NA_EXPORT', f3z.strings(manifest_dat()))


class NotBuiltTests(Base):
    def assert_not_built(self, reason):
        tally = self.build()
        self.assertEqual((tally['built'], tally['not_built']), (0, 1))
        self.assertEqual(self.asm.get('f3z_not_built'), reason)
        self.assertIsNone(self.asm.get('f3z'))
        self.assertEqual(sorted(p.suffix for p in (self.root / 'Hub' / 'Proj').iterdir()), ['.f3d'] * len(
            list((self.root / 'Hub' / 'Proj').iterdir())))  # nothing written, no temporary file left

    def test_missing_inserted_design(self):
        self.asm = self.folder.design('asm', 'Box', references=[ref('gone', 'Gone')])
        self.assert_not_built(f3z.CHILD_VERSION_MISSING % 1)

    def test_other_version_only(self):
        self.folder.design('pin', 'Pin', version=2)
        self.asm = self.folder.design('asm', 'Box', references=[ref('pin', 'Pin', 1)])
        self.assert_not_built(f3z.CHILD_VERSION_MISSING % 1)

    def test_out_of_date_links_without_versions(self):
        self.folder.design('pin', 'Pin', version=2)
        self.asm = self.folder.design('asm', 'Box', references=[dict(id=LINEAGE + 'pin', name='Pin')], out_of_date=True)
        self.assert_not_built(f3z.OUT_OF_DATE)

    def test_link_without_version_takes_the_newest(self):
        self.folder.design('pin', 'Pin', version=1)
        newest = self.folder.design('pin', 'Pin', version=2)
        self.asm = self.folder.design('asm', 'Box', references=[dict(id=LINEAGE + 'pin', name='Pin')])
        self.assertEqual(self.build()['built'], 1)
        _, graph = self.graph()
        self.assertEqual([item['version'] for item in graph['designObjects']], [1, 2])
        self.assertFalse(self.asm['f3z']['members'][1]['pinned'])
        self.assertEqual(self.asm['f3z']['members'][1]['sha256'], newest['files'][0]['sha256'])

    def test_inserted_design_changed_on_disk(self):
        self.bracket()
        (self.root / self.pin['files'][0]['path']).write_bytes(b'changed by hand')
        self.assert_not_built(f3z.CHILD_CHANGED)

    def test_another_file_has_its_name(self):
        self.bracket()
        (self.root / 'Hub/Proj/Bracket v1.f3z').write_bytes(b'the user\'s own file')
        tally = self.build()
        self.assertEqual(tally['not_built'], 1)
        self.assertEqual(self.asm['f3z_not_built'], f3z.TAKEN)
        self.assertEqual((self.root / 'Hub/Proj/Bracket v1.f3z').read_bytes(), b'the user\'s own file')

    def test_edited_bundle_is_never_written_over(self):
        self.bracket()
        self.build()
        path = self.root / self.asm['f3z']['path']
        path.write_bytes(b'edited')
        self.pin['files'][0]['sha256'] = 'changed'  # a member changed: it would be rebuilt
        self.build()
        self.assertEqual(self.asm['f3z_not_built'], f3z.EDITED)
        self.assertEqual(path.read_bytes(), b'edited')


class ZstdTests(Base):
    def test_without_zstd_built_without_roles_then_with_them(self):
        self.pin = self.folder.design('pin', 'Pin')
        data = zstd_header(f3d_bytes([(ROLE_PIN, LINEAGE + 'pin', 1, 'Pin')], 'asm', method=zipfile.ZIP_STORED))
        self.asm = self.folder.design('asm', 'Box', data=data, references=[ref('pin', 'Pin')])
        with mock.patch.object(f3z, 'zstd_readable', return_value=False):
            self.assertIsNone(f3z.read_roles(self.root / self.asm['files'][0]['path']))
            tally = self.build()
            self.assertEqual((tally['built'], tally['without_roles']), (1, 1))
            self.assertFalse(self.asm['f3z']['roles'])
            _, graph = self.graph()
            self.assertEqual(graph['designObjects'][0]['references'][0]['relationships'][0]['metadata'], {})
            self.assertEqual(self.build()['up_to_date'], 1)
            self.assertTrue(any('neutronRole' in line for line in f3z.tally_lines(tally)))
        # A Python that reads Fusion's compression builds it again with the roles (here the member
        # is stored, only marked as Zstandard: reading it needs no decompression).
        with mock.patch.object(f3z, 'zstd_readable', return_value=True), \
                mock.patch.object(zipfile.ZipFile, 'read', lambda self, name, pwd=None: manifest_dat(
                    [(ROLE_PIN, LINEAGE + 'pin', 1, 'Pin')])):
            tally = self.build('delete')
        self.assertEqual(tally['built'], 1)
        self.assertTrue(self.asm['f3z']['roles'])
        _, graph = self.graph()
        self.assertEqual(graph['designObjects'][0]['references'][0]['relationships'][0]['metadata'],
                         dict(neutronRole=ROLE_PIN))

    @unittest.skipUnless(f3z.zstd_readable(), 'this Python cannot write Zstandard')
    def test_real_zstandard_manifest(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(zipfile.ZipInfo('Manifest.dat'), manifest_dat([(ROLE_PIN, LINEAGE + 'p', 1, 'P')]),
                             compress_type=f3z.ZSTANDARD)
        path = self.root / 'a.f3d'
        path.write_bytes(stream.getvalue())
        self.assertEqual(f3z.read_roles(path), {LINEAGE + 'p': ROLE_PIN})


class RebuildTests(Base):
    def test_up_to_date_then_rebuilt_when_a_member_changes(self):
        self.bracket()
        self.assertEqual(self.build()['built'], 1)
        first = dict(self.asm['f3z'])
        self.assertEqual(self.build()['up_to_date'], 1)
        # The Pin's version 1 is exported again (new bytes): the bundle no longer holds it.
        path = self.root / self.pin['files'][0]['path']
        path.write_bytes(f3d_bytes(tag='pin again'))
        blob = path.read_bytes()
        self.pin['files'][0].update(bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())
        self.assertEqual(f3z.folder_counts(self.folder.manifest)['stale'], 1)
        tally = self.build('move')
        self.assertEqual((tally['built'], tally['moved']), (1, 1))
        older = self.asm['f3z_older'][0]
        self.assertTrue(older['moved_to'].startswith('Older versions/Hub/Proj/Bracket v1 ('))
        self.assertEqual(checks.file_sha256(self.root / older['moved_to']), first['sha256'])
        self.assertNotEqual(self.asm['f3z']['sha256'], first['sha256'])
        self.assertEqual(self.bundle().read(json.loads(self.bundle().read('Manifest.json'))['root']),
                         (self.root / self.asm['files'][0]['path']).read_bytes())

    def change_pin(self):
        path = self.root / self.pin['files'][0]['path']
        path.write_bytes(f3d_bytes(tag='pin %d' % len(self.asm.get('f3z_older') or [])))
        blob = path.read_bytes()
        self.pin['files'][0].update(bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())

    def test_keep_and_delete(self):
        self.bracket()
        self.build()
        self.change_pin()
        self.assertEqual(self.build('keep')['kept'], 1)
        kept = self.asm['f3z_older'][0]['kept_as']
        self.assertTrue(kept.startswith('Hub/Proj/Bracket v1 (') and kept.endswith(').f3z'))
        self.assertTrue((self.root / kept).is_file())
        self.change_pin()
        self.assertEqual(self.build('delete')['removed'], 1)
        self.assertIn('removed', self.asm['f3z_older'][1])
        self.assertEqual(sorted(p.name for p in (self.root / 'Hub' / 'Proj').glob('*.f3z')),
                         sorted(['Bracket v1.f3z', kept.rsplit('/', 1)[1]]))

    def test_moved_older_assembly_takes_its_bundle_along(self):
        root = self.root
        adapter = FusionAdapter(NS(documents=fake.Documents()))
        assembly = fake.File('Box', 'asm', references=1, version=1)
        part = assembly.childReferences.items[0]
        exporter = Exporter(root, adapter)
        exporter.run(fake.data([part, assembly]), f3z_bundles=True)
        first = next(e for e in exporter.manifest['entries'] if e['id'] == 'asm')
        self.assertEqual(first['f3z']['path'].rsplit('/', 1)[1], 'Box v1.f3z')
        assembly.versionNumber = assembly.latestVersionNumber = 2
        exporter = Exporter(root, adapter)
        exporter.run(fake.data([part, assembly]), f3z_bundles=True)
        old = next(e for e in exporter.manifest['entries'] if e['id'] == 'asm' and e['version'] == 1)
        self.assertEqual(old['status'], 'moved')
        self.assertNotIn('f3z', old)
        moved = [info for info in old['moved_files'] if info['role'] == 'f3z_bundle']
        self.assertEqual(len(moved), 1)
        self.assertTrue((root / moved[0]['moved_to']).is_file())
        new = next(e for e in exporter.manifest['entries'] if e['id'] == 'asm' and e['version'] == 2)
        self.assertTrue((root / new['f3z']['path']).is_file())


class ExportTests(Base):
    def test_option_on_and_off(self):
        adapter = FusionAdapter(NS(documents=fake.Documents()))
        assembly = fake.File('Box', 'asm', references=1)
        part = assembly.childReferences.items[0]
        data = fake.data([part, assembly])
        exporter = Exporter(self.root, adapter)
        counts = exporter.run(data)
        notes = report.summary(counts, exporter.run_info, exporter.manifest, self.root)['notes']
        self.assertTrue(any(note.endswith('To build an .f3z beside it, run the export again with "Also build .f3z '
                                          'for assemblies" checked, or: python3 scripts/autorun.py bundle <folder>.')
                            for note in notes))
        self.assertFalse(list(self.root.rglob('*.f3z')))
        archives = {path: path.read_bytes() for path in self.root.rglob('*.f3d')}
        exporter = Exporter(self.root, adapter)
        counts = exporter.run(data, f3z_bundles=True)
        self.assertEqual(exporter.run_info['f3z']['built'], 1)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*.f3d')}, archives)
        notes = report.summary(counts, exporter.run_info, exporter.manifest, self.root)['notes']
        self.assertTrue(any(note.endswith('every inserted design is in this folder too. Built 1 .f3z file beside it.')
                            for note in notes), notes)
        page, table = report.write(exporter)
        self.assertIn('<a href="Hub/Project/Box%20v3.f3z">F3Z</a>', page.read_text(encoding='utf-8'))
        rows = {row['design']: row for row in csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig')))}
        self.assertEqual(rows['Box']['f3z'], 'Hub/Project/Box v3.f3z')
        exporter = Exporter(self.root, adapter)
        exporter.run(data, f3z_bundles=True)
        self.assertEqual(exporter.run_info['f3z']['up_to_date'], 1)

    def test_a_folder_from_an_earlier_version_gets_link_versions_on_the_next_run(self):
        # Links recorded before they carried a version: the next run reads them again for designs
        # that are up to date (nothing re-exported), so an .f3z can then match exact versions.
        adapter = FusionAdapter(NS(documents=fake.Documents()))
        assembly = fake.File('Box', 'asm', references=1)
        part = assembly.childReferences.items[0]
        data = fake.data([part, assembly])
        Exporter(self.root, adapter).run(data)
        path = self.root / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        box = [entry for entry in manifest['entries'] if entry['name'] == 'Box'][0]
        self.assertIsNotNone(box['references'][0].get('version'))
        for link in box['references']:
            link.pop('version', None)
        path.write_bytes(json.dumps(manifest).encode('utf-8'))
        archives = {item: item.read_bytes() for item in self.root.rglob('*.f3d')}
        exporter = Exporter(self.root, adapter)
        counts = exporter.run(data, f3z_bundles=True)
        self.assertEqual(counts['exported'], 0)
        box = [entry for entry in exporter.manifest['entries'] if entry['name'] == 'Box'][0]
        self.assertIsNotNone(box['references'][0].get('version'))
        self.assertEqual(exporter.run_info['f3z']['built'], 1)
        self.assertEqual({item: item.read_bytes() for item in self.root.rglob('*.f3d')}, archives)

    def test_not_built_is_in_the_report_and_summary(self):
        self.asm = self.folder.design('asm', 'Box', references=[ref('gone', 'Gone')])
        self.build()
        manifest = self.folder.manifest
        run_info = dict(f3z_bundles=True)
        notes = report.summary({}, run_info, manifest, self.root)['notes']
        self.assertTrue(any(note.endswith('1 could not be built; the report says why.') for note in notes), notes)
        html = report.render_html(manifest, run_info, {})
        self.assertIn('.f3z not built: version 1 of an inserted design', html)
        rows = list(csv.DictReader(io.StringIO(report.render_csv(manifest, run_info))))
        self.assertTrue(rows[0]['f3z'].startswith('(.f3z not built: '))


class LauncherTests(Base):
    def run_cli(self, *argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = launcher.main(list(argv))
        return code, stream.getvalue()

    def test_bundle_subcommand_then_check(self):
        self.bracket()
        self.folder.save()
        code, out = self.run_cli('bundle', str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn('.f3z for 1 assembly: built 1 now, 0 already up to date', out)
        self.assertNotIn('Bracket', out)
        self.assertTrue((self.root / 'Hub/Proj/Bracket v1.f3z').is_file())
        self.assertTrue((self.root / 'index.html').is_file())
        manifest = checks.read_manifest(self.root)
        entry = next(e for e in manifest['entries'] if e['name'] == 'Bracket')
        self.assertEqual(entry['f3z']['path'], 'Hub/Proj/Bracket v1.f3z')
        code, out = self.run_cli('bundle', str(self.root))
        self.assertIn('built 0 now, 1 already up to date', out)
        code, out = self.run_cli('check', str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn('Built 1 .f3z file beside it.', out)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])['f3z_bundles'], 1)
        status = '\n'.join(launcher.summary(self.root))
        self.assertIn('Built 1 .f3z file beside it.', status)
        self.assertNotIn('Bracket', status)
        # A changed .f3z is a problem for check.
        (self.root / 'Hub/Proj/Bracket v1.f3z').write_bytes(b'broken')
        code, out = self.run_cli('check', str(self.root))
        self.assertEqual(code, 1)
        self.assertIn('PROBLEM file changed since it was built', out)

    def test_bundle_without_a_manifest_or_while_an_export_runs(self):
        code, out = self.run_cli('bundle', str(self.root))
        self.assertEqual(code, 1)
        self.assertIn('no manifest.json', out)
        self.bracket()
        self.folder.save()
        from fusion_bulk_export.lock import FolderLock
        lock = FolderLock(self.root)
        lock.acquire()
        try:
            code, out = self.run_cli('bundle', str(self.root))
        finally:
            lock.release()
        self.assertEqual(code, 1)
        self.assertIn('Another export is using this folder', out)
        self.assertFalse((self.root / 'Hub/Proj/Bracket v1.f3z').exists())

    def test_export_flag_goes_into_the_job(self):
        installed = self.root / 'addin'
        installed.mkdir()
        with mock.patch.object(launcher, 'update_addin'), mock.patch.object(launcher, 'exporting', return_value=False):
            launcher.install(installed, self.root / 'out', 'export', f3z_bundles=True)
            self.assertIs(json.loads((installed / 'autorun.json').read_text())['f3z_bundles'], True)
            launcher.install(installed, self.root / 'out', 'export')
            self.assertNotIn('f3z_bundles', json.loads((installed / 'autorun.json').read_text()))

    def test_job_key(self):
        self.assertIs(autorun.check_settings({})[-2], False)
        self.assertIs(autorun.check_settings(dict(f3z_bundles=True))[-2], True)
        with self.assertRaises(ValueError):
            autorun.check_settings(dict(f3z_bundles='yes'))


class DialogTests(UIBase):
    """The dialog's "Also build .f3z for assemblies" box: off by default, its note, remembered."""
    def test_the_box_is_off_by_default_with_its_note_and_is_remembered(self):
        from fusion_bulk_export import picker
        assembly = fake.File('Box', 'asm', references=1)
        self.app.data = fake.data([assembly.childReferences.items[0], assembly])
        command = self.open_dialog()
        box, note = command.commandInputs.find('fbx_f3z'), command.commandInputs.find('fbx_f3z_note')
        self.assertEqual(box.name, 'Also build .f3z for assemblies')
        self.assertFalse(box.value)
        self.assertEqual((note.text, note.isVisible, note.isFullWidth),
                         ('A single file with its inserted designs, beside the .f3d.', True, True))
        self.assertLessEqual(len(note.text), len(picker.STEP_OFF_NOTE))  # one line, as the STEP note
        command.execute.handlers[0].notify(None)
        self.assertFalse(list(Path(self.temp.name).rglob('*.f3z')))
        self.assertIs(json.loads(self.settings.read_text(encoding='utf-8'))['f3z_bundles'], False)
        command = self.open_dialog()
        command.commandInputs.find('fbx_f3z').value = True
        command.execute.handlers[0].notify(None)
        self.assertEqual(len(list(Path(self.temp.name).rglob('*.f3z'))), 1)
        self.assertIs(json.loads(self.settings.read_text(encoding='utf-8'))['f3z_bundles'], True)
        self.assertTrue(self.open_dialog().commandInputs.find('fbx_f3z').value)


if __name__ == '__main__':
    unittest.main()

class ModeTests(Base):
    @unittest.skipIf(os.name == 'nt', 'POSIX file modes')
    def test_the_bundle_gets_the_usual_mode_not_owner_only(self):
        mask = os.umask(0o022)
        self.addCleanup(os.umask, mask)
        asm = self.bracket()
        self.folder.save()
        self.build()
        path = self.root / asm['f3z']['path']
        self.assertEqual(os.stat(str(path)).st_mode & 0o777, 0o644)
