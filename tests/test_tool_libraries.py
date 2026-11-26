"""CAM tool libraries (toollibs.py): each of the user's own tool libraries saved once per run, at its
start, as "Tool libraries/<location>/<folders>/<name>.json"; Autodesk's samples skipped; a missing
or failing CAM API a warning, never a failed export; up to date when unchanged; a changed one's
previous copy moved, kept or deleted by the "Older versions" choice; checked by `check`."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import autorun, checks, command, core, report, toollibs  # noqa: E402
from fusion_bulk_export.core import Exporter  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402
from tests.test_ui import UIBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('autorun_launcher_tools', ROOT / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def account():
    """Libraries in four locations, nested folders, and Autodesk's samples."""
    return {
        'LocalLibraryLocation': {'Mills': fake.ToolLibrary(3),
                                 'Shop': {'Lathe': fake.ToolLibrary(2), 'Deep': {'Drills': fake.ToolLibrary(5)}}},
        'CloudLibraryLocation': {'Router bits': fake.ToolLibrary(4)},
        'HubLibraryLocation': {'Team': {'Shared mills': fake.ToolLibrary(1)}},
        'Fusion360LibraryLocation': {'Samples': {'Milling Tools (Metric)': fake.ToolLibrary(50)}},
        'NetworkLibraryLocation': {'Sample folder': {'Network mills': fake.ToolLibrary(3)}},
        'OnlineSamplesLibraryLocation': {'Online mills': fake.ToolLibrary(9)},
    }


class ToolLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))
        self.part = fake.File('Part', 'part')
        self.locations = account()
        self.libraries = fake.ToolLibraries(self.locations)
        self.cam = fake.cam_module(self.libraries)

    def run_export(self, cam=True, **kwargs):
        kwargs.setdefault('tool_libraries', True)
        self.exporter = Exporter(self.root, self.adapter)
        if cam is True:
            self.exporter.cam = self.cam
        elif cam is not None:
            self.exporter.cam = cam
        self.counts = self.exporter.run(fake.data([self.part]), **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.tally = self.exporter.run_info.get('tool_libraries')
        return self.counts

    def library_files(self, older=False):
        base = self.root / ('Older versions/Tool libraries' if older else 'Tool libraries')
        return sorted(p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file())

    def entry(self, name):
        return next(e for e in self.manifest['tool_libraries'] if e['name'] == name)

    def notes(self):
        return report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)['notes']

    def test_every_library_of_the_users_own_locations_with_their_folders(self):
        self.run_export()
        self.assertEqual(self.library_files(), ['Cloud/Router bits.json', 'Hub/Team/Shared mills.json',
                                                'Local/Mills.json', 'Local/Shop/Deep/Drills.json',
                                                'Local/Shop/Lathe.json'])
        # The file is what toJson() gave, as it is.
        lathe = self.locations['LocalLibraryLocation']['Shop']['Lathe']
        self.assertEqual((self.root / 'Tool libraries/Local/Shop/Lathe.json').read_text(encoding='utf-8'),
                         lathe.toJson())
        entry = self.entry('Drills')
        self.assertEqual((entry['location'], entry['folder'], entry['path'], entry['tools'], entry['status']),
                         ('Local', ['Shop', 'Deep'], 'Tool libraries/Local/Shop/Deep/Drills.json', 5, 'done'))
        self.assertEqual(entry['url_scheme'], 'toollibraryroot')  # only the scheme: never the URL itself
        self.assertEqual(entry['sha256'], checks.file_sha256(self.root / entry['path']))
        self.assertEqual(entry['bytes'], (self.root / entry['path']).stat().st_size)
        self.assertTrue(entry['exported'].startswith('20'))
        self.assertIsNone(entry['error'])
        self.assertNotIn('://', json.dumps(self.manifest['tool_libraries']))
        self.assertEqual((self.tally['exported'], self.tally['tools_exported'], self.tally['failed']), (5, 15, 0))
        self.assertIn('Exported 5 tool libraries (15 tools).', self.notes())
        # The design is exported as ever, and the libraries are not counted as designs or files.
        self.assertEqual((self.counts['exported'], self.counts['files']), (1, 0))
        self.assertEqual(len(self.manifest['entries']), 1)

    def test_autodesks_sample_libraries_are_skipped(self):
        self.run_export()
        self.assertNotIn('Fusion360LibraryLocation', self.libraries.asked)  # not even listed
        self.assertNotIn('OnlineSamplesLibraryLocation', self.libraries.asked)
        self.assertNotIn('NetworkLibraryLocation', self.libraries.asked)  # no scheme, samples on real Fusion
        self.assertFalse([e for e in self.manifest['tool_libraries'] if 'mills' == e['name'].lower()[-5:]
                          and e['location'] not in ('Local', 'Hub')])
        self.assertEqual(self.locations['Fusion360LibraryLocation']['Samples']['Milling Tools (Metric)'].json_calls, 0)
        self.assertEqual(self.tally['skipped_locations'], ['Network', 'Fusion samples', 'Online samples'])

    def test_libraries_are_saved_before_the_first_design(self):
        order = []
        real = self.adapter.export
        self.adapter.export = lambda file, stem: order.append('design') or real(file, stem)
        library = self.locations['CloudLibraryLocation']['Router bits']
        real_json = library.toJson
        library.toJson = lambda: order.append('library') or real_json()
        self.run_export()
        self.assertEqual(order[:2], ['library', 'design'])

    def test_a_fusion_without_the_cam_api_exports_the_designs_with_a_warning(self):
        with patch.dict(sys.modules, {'adsk.cam': None}):  # "import adsk.cam" fails
            self.run_export(cam=None)
        self.assertEqual((self.counts['exported'], self.counts['failed']), (1, 0))
        self.assertEqual(self.tally['warning'], toollibs.NO_CAM)
        self.assertFalse((self.root / 'Tool libraries').exists())
        self.assertIn('Tool libraries were not exported. ' + toollibs.NO_CAM, self.notes())
        self.assertIsNone(self.exporter.run_info['stopped_reason'])
        page = (report.render_html(self.manifest, self.exporter.run_info, self.counts))
        self.assertIn('Not exported in the last run.', page)
        # An older adsk.cam without CAMManager, and one whose libraries raise, the same.
        self.run_export(cam=fake.cam_module(self.libraries, manager=False))
        self.assertEqual(self.tally['warning'], toollibs.NO_CAM)
        broken = fake.cam_module(self.libraries)
        broken.CAMManager = NS(get=lambda: (_ for _ in ()).throw(RuntimeError('3 : no CAM')))
        self.run_export(cam=broken)
        self.assertEqual(self.tally['warning'], "Fusion's tool libraries could not be opened.")
        self.assertEqual(self.counts['failed'], 0)

    def test_anything_unexpected_while_listing_is_a_warning_too(self):
        self.libraries.childAssetURLs = None  # not callable: a TypeError inside the listing
        self.run_export()
        self.assertEqual(self.counts['exported'], 1)
        self.assertTrue(self.tally['unreadable'])
        self.assertFalse(self.tally['listed'])

    def test_a_location_that_cannot_be_read_leaves_the_others(self):
        self.libraries.failing.add('HubLibraryLocation')
        self.run_export()
        self.assertNotIn('Hub', [e['location'] for e in self.manifest['tool_libraries']])
        self.assertEqual(self.tally['exported'], 4)
        self.assertEqual(self.tally['unreadable'], [dict(location='Hub', error='3 : location not available')])
        self.assertIn('1 location or folder of tool libraries could not be read.', self.notes())
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('Could not be read in Hub: 3 : location not available', page)

    def test_a_location_fusion_says_holds_nothing_is_no_problem(self):
        self.libraries.empty.update(['CloudLibraryLocation', 'HubLibraryLocation'])
        self.run_export()
        self.assertEqual((self.tally['exported'], self.tally['unreadable']), (3, []))
        self.assertTrue(self.tally['listed'])
        self.assertFalse([note for note in self.notes() if 'could not be read' in note])

    def test_an_empty_library_is_exported_too(self):
        self.libraries.locations = dict(LocalLibraryLocation={'Library': fake.ToolLibrary(0)})
        self.run_export()
        self.assertEqual(self.entry('Library')['tools'], 0)
        self.assertIn('Exported 1 tool library (0 tools).', self.notes())

    def test_one_library_that_will_not_hand_out_its_contents_fails_alone(self):
        self.locations['LocalLibraryLocation']['Mills'].error = RuntimeError('2 : toJson failed')
        self.run_export()
        mills = self.entry('Mills')
        self.assertEqual((mills['status'], mills['error']), ('failed', '2 : toJson failed'))
        self.assertNotIn('path', mills)
        self.assertEqual((self.tally['exported'], self.tally['failed']), (4, 1))
        self.assertIn('1 tool library could not be exported; the report says why.', self.notes())
        self.assertEqual(self.counts['failed'], 0)  # designs: none failed
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('2 : toJson failed', page)
        rows = list(csv.reader(io.StringIO(report.render_csv(self.manifest, self.exporter.run_info))))
        header = rows[0]
        mills_row = next(dict(zip(header, row)) for row in rows[1:] if row[header.index('design')] == 'Mills')
        self.assertEqual((mills_row['status'], mills_row['kind'], mills_row['reason'], mills_row['location']),
                         ('failed', 'tool_library', '2 : toJson failed', 'Local'))
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['tool_libraries'], totals['tool_libraries_failed'], totals['failed_checks']), (4, 1, 1))
        # Once it works again, it is exported; a later failure keeps the file it had.
        self.locations['LocalLibraryLocation']['Mills'].error = None
        self.run_export()
        self.assertEqual(self.entry('Mills')['status'], 'done')
        self.assertEqual(self.tally['exported'], 1)
        self.locations['LocalLibraryLocation']['Mills'].error = RuntimeError('2 : again')
        self.run_export()
        mills = self.entry('Mills')
        self.assertEqual((mills['status'], mills['path']), ('failed', 'Tool libraries/Local/Mills.json'))
        self.assertTrue((self.root / mills['path']).is_file())

    def test_a_second_run_finds_them_up_to_date_and_writes_nothing(self):
        self.run_export()
        times = {p: p.stat().st_mtime_ns for p in (self.root / 'Tool libraries').rglob('*.json')}
        self.run_export()
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.root / 'Tool libraries').rglob('*.json')}, times)
        self.assertEqual((self.tally['exported'], self.tally['up_to_date']), (0, 5))
        self.assertIn('5 tool libraries already up to date.', self.notes())
        self.assertFalse([note for note in self.notes() if note.startswith('Exported')])
        self.assertFalse((self.root / 'Older versions').exists())

    def changed_mills(self, older_versions):
        self.run_export(older_versions=older_versions)
        before = self.entry('Mills')
        self.locations['LocalLibraryLocation']['Mills'].tools = 4  # a tool added in Fusion
        self.run_export(older_versions=older_versions)
        return before, self.entry('Mills')

    def test_a_changed_library_moves_its_previous_copy_to_older_versions_by_default(self):
        self.run_export()
        before = self.entry('Mills')
        self.locations['LocalLibraryLocation']['Mills'].tools = 4
        self.run_export()
        after = self.entry('Mills')
        day = before['exported'][:10]
        self.assertEqual(self.library_files(older=True), ['Local/Mills (%s).json' % day])
        moved = self.root / ('Older versions/Tool libraries/Local/Mills (%s).json' % day)
        self.assertEqual(checks.file_sha256(moved), before['sha256'])
        self.assertEqual((after['path'], after['tools']), ('Tool libraries/Local/Mills.json', 4))
        self.assertNotEqual(after['sha256'], before['sha256'])
        self.assertEqual(after['older'][0]['moved_to'], 'Older versions/Tool libraries/Local/Mills (%s).json' % day)
        self.assertEqual((self.tally['exported'], self.tally['up_to_date'], self.tally['moved']), (1, 4, 1))
        self.assertIn('Exported 1 tool library (4 tools).', self.notes())
        self.assertIn('Changed tool libraries: 1 previous copy moved to Older versions.', self.notes())
        # Changed again the same day: the name is taken, so " (2)".
        self.locations['LocalLibraryLocation']['Mills'].tools = 6
        self.run_export()
        self.assertEqual(len(self.library_files(older=True)), 2)
        self.assertIn('Local/Mills (%s) (2).json' % day, self.library_files(older=True))
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)

    def test_a_changed_library_with_keep_keeps_its_previous_copy_beside_it(self):
        before, after = self.changed_mills('keep')
        day = before['exported'][:10]
        self.assertIn('Local/Mills (%s).json' % day, self.library_files())
        self.assertIn('Local/Mills.json', self.library_files())
        self.assertEqual(after['older'][0]['kept_as'], 'Tool libraries/Local/Mills (%s).json' % day)
        self.assertFalse((self.root / 'Older versions').exists())
        self.assertIn('Changed tool libraries: 1 previous copy kept beside the new one.', self.notes())

    def test_a_changed_library_with_delete_replaces_its_previous_copy(self):
        before, after = self.changed_mills('delete')
        self.assertEqual([name for name in self.library_files() if 'Mills' in name], ['Local/Mills.json'])
        self.assertFalse((self.root / 'Older versions').exists())
        self.assertTrue(after['older'][0]['removed'])
        self.assertEqual(after['older'][0]['sha256'], before['sha256'])
        self.assertIn('Changed tool libraries: 1 previous copy replaced.', self.notes())

    def test_a_file_changed_by_hand_is_never_written_over_or_moved(self):
        self.run_export()
        path = self.root / 'Tool libraries/Local/Mills.json'
        path.write_text('{"data": [], "mine": true}', encoding='utf-8')
        self.locations['LocalLibraryLocation']['Mills'].tools = 4
        self.run_export()
        self.assertEqual(path.read_text(encoding='utf-8'), '{"data": [], "mine": true}')
        mills = self.entry('Mills')
        self.assertTrue(mills['path'].startswith('Tool libraries/Local/Mills~'))
        self.assertEqual(mills['older'][0]['left'], toollibs.KEPT_EDITED)
        self.assertFalse((self.root / 'Older versions').exists())
        self.run_export()  # and then it is up to date under its own name
        self.assertEqual(self.tally['exported'], 0)

    def test_a_library_gone_from_fusion_keeps_its_file_and_entry(self):
        self.run_export()
        del self.locations['CloudLibraryLocation']['Router bits']
        self.run_export(older_versions='delete')
        self.assertTrue((self.root / 'Tool libraries/Cloud/Router bits.json').is_file())
        self.assertEqual(self.entry('Router bits')['status'], 'done')
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('Not found in Fusion in the last run; its file is kept.', page)
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)

    def test_a_file_already_there_with_the_same_contents_is_taken_over(self):
        # A run that stopped after writing but before recording: the next one does not add "~1a2b".
        target = self.root / 'Tool libraries/Cloud/Router bits.json'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.locations['CloudLibraryLocation']['Router bits'].toJson().encode('utf-8'))
        mine = self.root / 'Tool libraries/Local/Mills.json'
        mine.parent.mkdir(parents=True)
        mine.write_text('my own file', encoding='utf-8')
        self.run_export()
        self.assertEqual(self.entry('Router bits')['path'], 'Tool libraries/Cloud/Router bits.json')
        self.assertTrue(self.entry('Mills')['path'].startswith('Tool libraries/Local/Mills~'))
        self.assertEqual(mine.read_text(encoding='utf-8'), 'my own file')

    def test_off_unless_asked_for_and_a_hub_named_like_the_folder_goes_beside_it(self):
        self.run_export(tool_libraries=False)
        self.assertNotIn('tool_libraries', self.manifest)
        self.assertIsNone(self.tally)
        self.assertFalse([note for note in self.notes() if 'tool librar' in note.lower()])
        hub = fake.data([fake.File('Other', 'other')])
        hub.dataHubs.item(0).name = 'Tool libraries'
        exporter = Exporter(self.root, self.adapter)
        exporter.cam = self.cam
        exporter.run(hub, tool_libraries=True)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        other = next(e for e in manifest['entries'] if e['id'] == 'other')
        self.assertTrue(other['location'].startswith('Tool libraries~'))

    def test_only_the_libraries(self):
        self.run_export(designs=False)
        self.assertEqual(self.counts['exported'], 0)
        self.assertEqual(self.part.calls, 0)
        self.assertEqual(self.manifest['entries'], [])
        self.assertEqual(self.tally['exported'], 5)
        self.assertIs(self.exporter.run_info['designs'], False)

    def test_check_finds_a_missing_or_changed_library_file(self):
        self.run_export()
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)
        (self.root / 'Tool libraries/Local/Mills.json').unlink()
        with open(self.root / 'Tool libraries/Cloud/Router bits.json', 'a', encoding='utf-8') as stream:
            stream.write(' ')
        shown = []
        totals = checks.check_folder(self.root, show=lambda *args: shown.append(args))
        self.assertEqual((totals['failed_checks'], totals['missing'], totals['changed']), (2, 1, 1))
        problems = [problem for args in shown for problem in args[3]]
        self.assertIn('file missing: Tool libraries/Local/Mills.json', problems)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = launcher.check(self.root)
        self.assertEqual(status, 1)
        self.assertIn('tool library 1: PROBLEM file missing', out.getvalue())
        self.assertNotIn('Mills:', out.getvalue())  # labeled by number unless --names (as designs)
        self.assertIn('Of them 5 tool libraries (in Tool libraries), 0 failed in the last export.', out.getvalue())

    def test_progress_says_which_library(self):
        states = []
        self.run_export(progress=states.append)
        first = next(state for state in states if state['phase'] == 'tool_libraries')
        self.assertEqual(command.describe(first), 'Tool library 1: Mills\nLocal')
        self.assertEqual(command.bar(first)[1], 0)
        deep = next(state for state in states if state.get('name') == 'Drills')
        self.assertEqual(command.describe(deep), 'Tool library 3: Drills\nLocal / Shop / Deep')

    def test_plurals_and_no_brackets(self):
        notes = toollibs.summary_notes(dict(exported=1, tools_exported=1, up_to_date=1, failed=2, moved=2))
        self.assertEqual(notes, ['Exported 1 tool library (1 tool).', '1 tool library already up to date.',
                                 'Changed tool libraries: 2 previous copies moved to Older versions.',
                                 '2 tool libraries could not be exported; the report says why.'])
        self.assertEqual(toollibs.summary_notes(dict(exported=3, tools_exported=412)),
                         ['Exported 3 tool libraries (412 tools).'])
        self.assertEqual(toollibs.summary_notes(dict()), [])
        self.assertEqual(toollibs.summary_notes(dict(found=0)), ['No tool libraries of your own were found.'])
        for line in notes + launcher.tool_library_lines(dict(exported=2, tools_exported=3, kept=1, unreadable=1)):
            self.assertNotIn('(s)', line)
        self.assertEqual(launcher.tool_library_lines(dict(exported=2, tools_exported=3, kept=1, unreadable=1)),
                         ['Tool libraries: exported 2 tool libraries (3 tools), 1 previous copy kept beside, '
                          '1 location or folder could not be read'])

    def test_the_tool_count_comes_from_the_json_when_the_library_has_no_count(self):
        self.locations['CloudLibraryLocation']['Router bits'] = fake.ToolLibrary(7, count=False)
        self.locations['CloudLibraryLocation']['Odd'] = fake.ToolLibrary(1, text='not json', count=False)
        self.run_export()
        self.assertEqual(self.entry('Router bits')['tools'], 7)
        self.assertIsNone(self.entry('Odd')['tools'])
        self.assertEqual((self.root / self.entry('Odd')['path']).read_text(encoding='utf-8'), 'not json')


class ToolLibraryJobTests(unittest.TestCase):
    """The job's "tool_libraries" (true, false or "only") and the command line's flags."""
    def setUp(self):
        import adsk
        adsk.doEvents = lambda: None
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'out'
        self.config = self.root / 'autorun.json'
        self.part = fake.File('A', 'a')
        self.app = NS(version='test-version', documents=fake.Documents(), data=fake.data([self.part]),
                      userInterface=NS())
        self.locations = account()
        cam = patch.dict(sys.modules, {'adsk.cam': fake.cam_module(fake.ToolLibraries(self.locations))})
        cam.start()
        self.addCleanup(cam.stop)
        sleep = patch('fusion_bulk_export.autorun.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_with(self, **settings):
        settings.setdefault('output', str(self.output))
        self.config.write_text(json.dumps(settings))
        return autorun.run_autorun(self.app, self.config)

    def test_a_job_saves_them_by_default_and_not_when_off(self):
        result = self.run_with()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['tool_libraries_choice'], 'on')
        self.assertEqual((result['tool_libraries']['exported'], result['tool_libraries']['tools_exported']), (5, 15))
        self.assertEqual(result['counts']['exported'], 1)
        self.assertIn('Tool libraries', (self.output / 'index.html').read_text(encoding='utf-8'))
        lines = launcher.summary(self.output)
        self.assertIn('Tool libraries: exported 5 tool libraries (15 tools)', lines)
        self.assertIn('Tool libraries in this folder: 5 tool libraries (15 tools)', lines)
        self.assertFalse([line for line in lines if 'Mills' in line])  # counts only
        result = self.run_with(tool_libraries=False)
        self.assertEqual(result['tool_libraries_choice'], 'off')
        self.assertNotIn('tool_libraries', result)
        result = self.run_with(tool_libraries='sometimes')
        self.assertIn('tool_libraries must be true, false or "only"', result['errors'][0]['error'])

    def test_only_the_libraries_opens_no_design(self):
        result = self.run_with(tool_libraries='only')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['tool_libraries_choice'], 'only')
        self.assertEqual(result['tool_libraries']['exported'], 5)
        self.assertEqual(result['counts']['exported'], 0)
        self.assertEqual(self.part.calls, 0)
        self.assertEqual(sorted(p.name for p in self.output.rglob('*.f3d')), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(launcher.check(self.output), 0)

    def test_the_command_line_flags(self):
        installed = self.root / 'installed'
        installed.mkdir()
        job = lambda: json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))
        with patch.object(launcher.shutil, 'copytree'):
            launcher.install(installed, self.output, 'export')
            self.assertNotIn('tool_libraries', job())  # the add-in's default: on
            launcher.install(installed, self.output, 'export', tool_libraries=False)
            self.assertIs(job()['tool_libraries'], False)
            launcher.install(installed, self.output, 'export', tool_libraries='only')
            self.assertEqual(job()['tool_libraries'], 'only')
            launcher.install(installed, self.output, 'inventory', tool_libraries='only')
            self.assertNotIn('tool_libraries', job())  # only an export saves them
        seen = []
        with patch.object(launcher, 'start', side_effect=lambda args: seen.append(launcher.tool_libraries_choice(args))
                          or 0):
            launcher.main(['export', str(self.output)])
            launcher.main(['export', str(self.output), '--no-tool-libraries'])
            launcher.main(['export', str(self.output), '--tool-libraries-only'])
        self.assertEqual(seen, [True, False, 'only'])
        for wrong in (['--no-tool-libraries', '--tool-libraries-only'], ['--tool-libraries-only', '--project', 'p'],
                      ['--tool-libraries-only', '--limit', '3'], ['--tool-libraries-only', '--designs-only']):
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                launcher.main(['export', str(self.output)] + wrong)
        jobs = []
        with patch.object(launcher, 'install', side_effect=lambda *a: jobs.append(a)), \
                patch.object(launcher, 'alive', return_value=True), patch.object(launcher, 'wait', return_value=0):
            args = NS(command='export', project=None, limit=None, folder=None, designs_only=False, no_step=False,
                      older_versions='move', no_tool_libraries=False, tool_libraries_only=True, timeout=10)
            launcher.run_job(args, 'Darwin', self.output, installed)
        self.assertEqual(jobs[0][9], 'only')

    def test_a_warning_reaches_the_status_line(self):
        self.output.mkdir()
        (self.output / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', counts=dict(exported=1), errors=[], tool_libraries=dict(warning=toollibs.NO_CAM))),
            encoding='utf-8')
        self.assertIn('Tool libraries: not exported. ' + toollibs.NO_CAM, launcher.summary(self.output))


class ToolLibraryDialogTests(UIBase):
    """The dialog's "Tool libraries" box: on by default, its note, remembered."""
    def setUp(self):
        super().setUp()
        self.locations = account()
        cam = patch.dict(sys.modules, {'adsk.cam': fake.cam_module(fake.ToolLibraries(self.locations))})
        cam.start()
        self.addCleanup(cam.stop)

    def test_the_box_is_on_by_default_with_its_note_and_is_remembered(self):
        from fusion_bulk_export import picker
        command = self.open_dialog()
        inputs = command.commandInputs
        box, note = inputs.find('fbx_tools'), inputs.find('fbx_tools_note')
        self.assertEqual(box.name, 'Tool libraries')
        self.assertTrue(box.value)
        self.assertIn("not Autodesk's sample libraries", box.tooltip)
        self.assertEqual((note.text, note.isVisible, note.isFullWidth),
                         ('Your CAM tool libraries, one .json file each.', True, True))
        self.assertLessEqual(len(note.text), len(picker.STEP_OFF_NOTE))  # one line, as the STEP note
        command.execute.handlers[0].notify(None)
        self.assertIn('Exported 5 tool libraries (15 tools).', self.results()[1]['notes'])
        self.assertTrue((Path(self.temp.name) / 'Tool libraries' / 'Local' / 'Mills.json').is_file())
        self.assertIs(json.loads(self.settings.read_text(encoding='utf-8'))['tool_libraries'], True)
        command = self.open_dialog()
        command.commandInputs.find('fbx_tools').value = False
        command.execute.handlers[0].notify(None)
        self.assertIs(json.loads(self.settings.read_text(encoding='utf-8'))['tool_libraries'], False)
        self.assertFalse([n for n in self.results()[1]['notes'] if 'tool librar' in n.lower()])
        self.assertFalse(self.open_dialog().commandInputs.find('fbx_tools').value)


if __name__ == '__main__':
    unittest.main()
