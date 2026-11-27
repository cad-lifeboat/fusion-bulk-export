"""Cross-platform launcher: paths, discovery, freshness, status privacy, stop."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from tests import LinkedTemporaryDirectory

spec = importlib.util.spec_from_file_location('autorun_launcher', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = LinkedTemporaryDirectory()  # as macOS and Windows temporary folders are
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_addins_dir_per_os(self):
        mac = launcher.addins_dir('Darwin', {'HOME': '/Users/u'})
        self.assertEqual(mac.as_posix(), '/Users/u/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns')
        win = launcher.addins_dir('Windows', {'APPDATA': '/appdata'})
        self.assertEqual(win.as_posix(), '/appdata/Autodesk/Autodesk Fusion 360/API/AddIns')

    def test_unsupported_os(self):
        with patch.object(launcher.platform, 'system', return_value='Linux'):
            with self.assertRaises(SystemExit):
                launcher.system()

    def test_windows_launcher_picks_newest(self):
        production = self.root / 'Autodesk' / 'webdeploy' / 'production'
        paths = []
        for index, name in enumerate(['old', 'new']):
            exe = production / name / 'FusionLauncher.exe'
            exe.parent.mkdir(parents=True)
            exe.write_text('')
            os.utime(exe, (1000 + index, 1000 + index))
            paths.append(exe)
        self.assertEqual(launcher.windows_launcher({'LOCALAPPDATA': str(self.root)}), paths[1])

    def test_windows_launcher_falls_back_to_fusion360(self):
        exe = self.root / 'Autodesk' / 'webdeploy' / 'production' / 'h' / 'Fusion360.exe'
        exe.parent.mkdir(parents=True)
        exe.write_text('')
        self.assertEqual(launcher.windows_launcher({'LOCALAPPDATA': str(self.root)}), exe)
        self.assertIsNone(launcher.windows_launcher({'LOCALAPPDATA': str(self.root / 'none')}))

    def test_alive_freshness(self):
        self.assertFalse(launcher.alive(self.root))
        marker = self.root / 'autorun.alive'
        marker.write_text('')
        now = marker.stat().st_mtime
        self.assertTrue(launcher.alive(self.root, now + 5))
        self.assertFalse(launcher.alive(self.root, now + 60))

    def test_windows_output_root_over_200_characters_is_refused_in_one_line(self):
        long = 'C:\\' + '\\'.join(['x' * 50] * 5)  # 257 characters
        problem = launcher.root_problem(long, 'Windows')
        self.assertIn('257 characters long; on Windows it can be at most 200', problem)
        self.assertIsNone(launcher.root_problem('C:\\' + 'y' * 197, 'Windows'))
        self.assertIsNone(launcher.root_problem('/' + 'z' * 300, 'Darwin'))  # no such rule on macOS
        with patch.object(launcher.sys, 'platform', 'win32'):  # the system named decides, not the host
            self.assertIsNone(launcher.root_problem('/' + 'z' * 300, 'Darwin'))
        args = launcher.argparse.Namespace(output=str(self.root / ('d' * 250)), command='export', project=None,
                                           limit=None, folder=None, designs_only=False, timeout=10)
        with patch.object(launcher, 'system', return_value='Windows'), \
                patch.object(launcher, 'root_problem', side_effect=lambda output, name: problem), \
                patch.object(launcher, 'addins_dir', side_effect=AssertionError('nothing is sent')):
            with self.assertRaises(SystemExit) as stop:
                launcher.start(args)
        self.assertEqual(stop.exception.code, problem)  # printed as one line, no traceback

    def test_an_output_folder_that_cannot_be_made_is_one_plain_line(self):
        blocker = self.root / 'file'
        blocker.write_text('x')
        with self.assertRaises(SystemExit) as stop:
            launcher.make_output(blocker / 'out')
        self.assertTrue(str(stop.exception.code).startswith('The output folder cannot be created: '))
        self.assertNotIn('\n', str(stop.exception.code))
        self.assertNotIn('Traceback', str(stop.exception.code))

    def test_long_output_path_is_allowed(self):
        long = self.root / ('x' * 150) / ('y' * 150)
        self.assertEqual(launcher.check_output(str(long)), long)

    def test_the_output_folder_keeps_the_path_it_was_given(self):
        # Not resolved: a mapped drive keeps its letter, a temporary folder its short or linked
        # name (macOS /var/folders, Windows RUNNER~1); "." and ".." are folded.
        folder = LinkedTemporaryDirectory()
        self.addCleanup(folder.cleanup)
        given = Path(folder.name) / 'out'
        self.assertEqual(launcher.check_output(str(given / 'sub' / '..')), given)
        if folder.linked:
            self.assertNotEqual(given, given.resolve())

    def test_status_reports_counts_without_names(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', finished='t', fusion_version='v', counts=dict(exported=1, up_to_date=3, files=0, skipped=0, failed=1), stopped_reason='Secret folder gone',
            stopped_code='output_missing',
            found=2, bytes=10, errors=[dict(stage='x', error='Secret Design failed')])))
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Design', status='done', files=[dict(bytes=4), dict(bytes=6)]),
            dict(name='Hidden Part', status='failed', files=[])])))
        (out / 'autorun.log').write_text('t1 project 1/1 design 2\nt1 design 2/2\nt2 could not read ["Private Hub"]: no access\n'
                                         't3 autorun: Traceback ... Hidden Part\n')
        text = '\n'.join(launcher.summary(out))
        for secret in ('Secret', 'Hidden', 'Private'):
            self.assertNotIn(secret, text)
        self.assertIn('Last progress: t1 project 1/1 design 2', text)
        self.assertFalse(launcher.SAFE_LOG.match('t design 2/2'))  # the form of earlier versions
        self.assertIn('"done": 1', text)
        self.assertIn('Counts: 1 exported now, 3 already up to date, 0 other files saved, 0 skipped, 1 failed', text)
        self.assertIn('Stopped by itself (output_missing)', text)
        self.assertIn('10 bytes', text)

    def test_status_shows_fusion_memory(self):
        out = self.root / 'out'
        out.mkdir()
        gb = 1 << 30
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', counts=dict(exported=1), memory=dict(start_rss=gb // 2, peak_rss=2 * gb, last_rss=3 * gb // 2))))
        self.assertIn('Fusion memory: 1.5 GB at the end (at most 2.0 GB, 0.5 GB at the start)', launcher.summary(out))
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={}, memory=None)))
        self.assertFalse([line for line in launcher.summary(out) if 'memory' in line])

    def test_the_fusion_memory_line_covers_every_run_into_the_folder(self):
        out = self.root / 'out'
        out.mkdir()
        gb = 1 << 30
        runs = [dict(memory=dict(start_rss=int(0.7 * gb), peak_rss=int(1.1 * gb), last_rss=int(0.8 * gb))),
                dict(),  # a run without figures (the synthetic API, an older version)
                dict(memory=dict(start_rss=int(0.8 * gb), peak_rss=int(1.0 * gb), last_rss=int(0.9 * gb)))]
        (out / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=runs, entries=[])))
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={}, memory=runs[2]['memory'])))
        lines = [line for line in launcher.summary(out) if line.startswith('Fusion memory')]
        self.assertEqual(lines, ['Fusion memory: 0.9 GB at the end (at most 1.1 GB over the 2 runs into this folder, '
                                 '0.7 GB at the start of the first)'])
        self.assertEqual(launcher.folder_memory_line(dict(runs=runs[:1])),
                         'Fusion memory: 0.8 GB at the end (at most 1.1 GB, 0.7 GB at the start)')

    def test_status_counts_link_problems_without_names(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Assembly', status='failed', files=[], has_references=True,
                 error='A linked design is missing or cannot be opened (1 link).', fusion_error='Secret link'),
            dict(name='Secret Older', status='done', files=[], references_out_of_date=True),
            dict(name='Secret Fine', status='done', files=[], references_out_of_date=False),
            dict(name='Secret Odd', status='done', files=[], references_unreadable=True, references_error='Secret')])))
        text = '\n'.join(launcher.summary(out))
        self.assertIn('Links: 1 design failed with a linked design missing or not opening', text)
        self.assertIn('Note: 1 design with out-of-date links (exported as it is, not a failure)', text)
        self.assertIn('Warning: 1 design whose links to other designs could not be read; it was exported all the same', text)
        self.assertNotIn('Secret', text)

    def test_check_command_runs_without_fusion(self):
        out = self.root / 'out'
        out.mkdir()
        step = out / 'a v1.step'
        step.write_bytes(b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('X'));\nENDSEC;\nDATA;\n" + b'/* x */\n' * 400
                         + b'ENDSEC;\nEND-ISO-10303-21;\n')
        import hashlib
        import zipfile
        with zipfile.ZipFile(out / 'a v1.f3d', 'w') as archive:
            archive.writestr('Manifest.dat', 'x')
            archive.writestr('FusionAssetName[Active]/x', 'x')
        files = [dict(path=p.name, bytes=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in (out / 'a v1.f3d', step)]
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Design', status='done', files=files, visible_solid_bodies=2,
                 warnings=['STEP has no bodies (Fusion showed 2)'])])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(launcher.main(['check', str(out)]), 0)
        text = stream.getvalue()
        self.assertIn('Checked 1 finished item: 0 with a problem, 0 missing files, 0 changed files, 1 STEP file missing bodies', text)
        self.assertIn('design 1: NOTE STEP has no bodies (Fusion showed 2)', text)
        self.assertNotIn('Secret', text)
        self.assertIn('Warnings: 1 item exported with a warning', '\n'.join(launcher.summary(out)))
        step.write_bytes(b'ISO-10303-21;\n')  # damaged since
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['check', str(out)]), 1)
            self.assertEqual(launcher.main(['check', str(self.root / 'nothing')]), 1)

    def test_status_reads_results_of_earlier_versions(self):
        self.assertEqual(launcher.count_line(dict(done=1, failed=0, skipped=2)),
                         'Counts: {"done": 1, "failed": 0, "skipped": 2}')

    def test_stop_and_status_commands(self):
        out = self.root / 'out'
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(launcher.main(['stop', str(out)]), 0)
            self.assertEqual(launcher.main(['status', str(out)]), 0)
        self.assertTrue((out / 'STOP').exists())
        self.assertIn('STOP is set', stream.getvalue())

    def test_install_writes_config(self):
        installed, out = self.root / 'AddIns' / 'FusionBulkExport', self.root / 'out'
        out.mkdir()
        (out / 'STOP').write_text('')
        installed.mkdir(parents=True)
        launcher.install(installed, out, 'export', ['p1'], 5)
        self.assertEqual(json.loads((installed / 'autorun.json').read_text(encoding='utf-8')),
                         dict(output=str(out), mode='export', projects=['p1'], limit=5))
        self.assertTrue((installed / 'FusionBulkExport.py').exists())
        # Every module of the add-in package is installed (a broad "autorun*" ignore once dropped one).
        source = Path(launcher.__file__).resolve().parents[1] / 'FusionBulkExport' / 'fusion_bulk_export'
        for module in source.glob('*.py'):
            self.assertTrue((installed / 'fusion_bulk_export' / module.name).is_file(), module.name)
        self.assertFalse((out / 'STOP').exists())

    def test_start_launches_fusion_without_touching_the_manifest(self):
        # Fusion's Run on Startup lives in Fusion's own settings, so the launcher leaves the
        # installed manifest exactly as shipped and relies on the hint when the add-in stays off.
        installed = self.root / 'AddIns' / 'FusionBulkExport'
        source = Path(launcher.__file__).resolve().parents[1] / 'FusionBulkExport' / 'FusionBulkExport.manifest'
        args = launcher.argparse.Namespace(output=str(self.root / 'out'), command='export', project=None,
                                           limit=None, folder=None, designs_only=False, timeout=10)
        launched, waited = [], []
        with patch.object(launcher, 'system', return_value='Darwin'), \
                patch.object(launcher, 'addins_dir', return_value=installed.parent), \
                patch.object(launcher, 'fusion_running', return_value=False), \
                patch.object(launcher, 'launch_fusion', side_effect=lambda name: launched.append(name)), \
                patch.object(launcher, 'wait', side_effect=lambda *a, **k: waited.append(k) or 0):
            self.assertEqual(launcher.start(args), 0)
        self.assertEqual(launched, ['Darwin'])
        self.assertEqual(waited, [{}])  # the default wait, which prints the hint if nothing starts
        self.assertEqual((installed / 'FusionBulkExport.manifest').read_bytes(), source.read_bytes())

    def test_wait_prints_manual_hint_once_then_reports(self):
        out, installed = self.root / 'out', self.root / 'inst'
        out.mkdir()
        installed.mkdir()
        ticks = iter(range(0, 10000, 100))
        calls = []

        def sleep(_):
            calls.append(1)
            if len(calls) == 5:
                (out / 'autorun-result.json').write_text(json.dumps(dict(
                    mode='inventory', counts=dict(designs=3), errors=[dict(stage='autorun', error='x')])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=sleep, clock=lambda: next(ticks))
        self.assertEqual(status, 1)
        self.assertEqual(stream.getvalue().count('has not started the add-in'), 1)
        self.assertIn('Check its Run on Startup box first, then switch Run on.', stream.getvalue())
        self.assertNotIn('Add-Ins tab', stream.getvalue())

    def test_wait_reports_add_in_startup_crash(self):
        out, installed = self.root / 'out', self.root / 'inst'
        out.mkdir()
        installed.mkdir()
        (installed / 'startup-error.txt').write_text('ModuleNotFoundError: x')
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)
        self.assertIn('ModuleNotFoundError', stream.getvalue())

    def test_wait_reports_rejected_job(self):
        out, installed = self.root / 'out', self.root / 'inst'
        (installed / 'autorun-output').mkdir(parents=True)
        out.mkdir()
        (installed / 'autorun-output' / 'autorun-result.json').write_text(json.dumps(dict(
            errors=[dict(stage='autorun', error='mode must be "export" or "inventory"')])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)
        self.assertIn('rejected the job', stream.getvalue())

    def test_wait_reports_an_output_folder_fusion_cannot_use_in_one_line(self):
        out, installed = self.root / 'out', self.root / 'inst'
        (installed / 'autorun-output').mkdir(parents=True)
        error = 'Fusion cannot use the output folder T:\\out (No such file or directory). A network drive must be connected for Fusion too; or choose another folder.'
        (installed / 'autorun-output' / 'autorun-result.json').write_text(json.dumps(dict(
            errors=[dict(stage='autorun', error=error, traceback='')])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)
        lines = stream.getvalue().splitlines()  # no "Waiting for" before a prompt answer
        self.assertEqual(lines, ['The add-in rejected the job: ' + error])

    def test_waiting_line_only_after_five_seconds(self):
        out, installed = self.root / 'out', self.root / 'inst'
        installed.mkdir()
        out.mkdir()
        ticks = iter(range(0, 100, 1))
        steps = []

        def sleep(_):
            steps.append(1)
            if len(steps) == 8:
                (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={}, errors=[])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch.object(launcher, 'alive', return_value=False):
            status = launcher.wait(installed, out, 86400, sleep=sleep, clock=lambda: next(ticks),
                                   hint_after=float('inf'))
        self.assertEqual(status, 0)
        self.assertEqual(sum(1 for line in stream.getvalue().splitlines() if line.startswith('Waiting for')), 1)

    def test_a_rejected_job_leaves_no_new_empty_output_folder(self):
        installed = self.root / 'installed'
        installed.mkdir()
        args = NS(command='export', project=None, limit=None, folder=None, designs_only=False, timeout=10)
        for existed, filled, status, kept in ((False, False, 1, False), (True, False, 1, True),
                                              (False, True, 1, True), (False, False, 0, True)):
            with self.subTest(existed=existed, filled=filled, status=status):
                out = self.root / ('out-%s-%s-%s' % (existed, filled, status))
                if existed:
                    out.mkdir()

                def answer(*_args, **_kwargs):
                    if filled:
                        (out / 'export.log').write_text('x')
                    return status
                with patch.object(launcher, 'update_addin'), patch.object(launcher, 'alive', return_value=True), \
                        patch.object(launcher, 'wait', side_effect=answer):
                    self.assertEqual(launcher.run_job(args, 'Darwin', out, installed), status)
                self.assertEqual(out.is_dir(), kept)

    def heartbeat(self, installed, busy):
        (installed / 'autorun.alive').write_text(json.dumps(dict(time=time.time(), busy=busy)))

    def test_wait_stops_when_fusion_took_the_job_and_answered_nowhere(self):
        out, installed = self.root / 'out', self.root / 'inst'
        installed.mkdir()
        out.mkdir()
        self.heartbeat(installed, None)  # alive, not exporting, and the job file is gone
        ticks = iter(range(0, 100000, 5))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: self.heartbeat(installed, None),
                                   clock=lambda: next(ticks), running=lambda: True)
        self.assertEqual(status, 1)
        self.assertEqual(stream.getvalue().splitlines()[-1], launcher.LOST_JOB_LINE)

    def test_wait_keeps_waiting_while_the_job_runs_or_is_not_taken_yet(self):
        out, installed = self.root / 'out', self.root / 'inst'
        installed.mkdir()
        out.mkdir()
        for busy, job in (('job', False), (None, True)):
            if job:
                (installed / 'autorun.json').write_text('{}')
            self.heartbeat(installed, busy)
            ticks = iter(range(0, 1000, 5))
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                launcher.wait(installed, out, 600, sleep=lambda _: self.heartbeat(installed, busy),
                              clock=lambda: next(ticks), running=lambda: True)
        # An add-in from before the heartbeat said whether it is busy: no guessing, it waits.
        (installed / 'autorun.json').unlink()
        (installed / 'autorun.alive').write_text('')
        ticks = iter(range(0, 1000, 5))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            launcher.wait(installed, out, 600, sleep=lambda _: (installed / 'autorun.alive').write_text(''),
                          clock=lambda: next(ticks), running=lambda: True)

    def test_reset_clears_a_stale_job_and_keeps_everything_else(self):
        installed = self.root / 'FusionBulkExport'
        installed.mkdir()
        for name in ('autorun.json', 'autorun.alive'):
            (installed / name).write_text('')
        manifest = json.dumps(dict(version='1.0.0', runOnStartup=False))
        (installed / 'FusionBulkExport.manifest').write_text(manifest)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launcher.reset(installed), 0)
        self.assertEqual(sorted(p.name for p in installed.iterdir()), ['FusionBulkExport.manifest', 'autorun.alive'])
        self.assertEqual((installed / 'FusionBulkExport.manifest').read_text(encoding='utf-8'), manifest)
        self.assertIn('button is always available', out.getvalue())

    def test_wait_reports_busy_fusion(self):
        out, installed = self.root / 'out', self.root / 'inst'
        out.mkdir()
        installed.mkdir()
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            busy=True, errors=[dict(stage='busy', error='An export started from the toolbar button is running')])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)
        self.assertIn('Fusion is busy: An export started', stream.getvalue())

    def test_busy_answer_is_read_beside_the_add_in_and_the_running_folder_is_left_alone(self):
        out, installed = self.root / 'out', self.root / 'inst'
        (installed / 'autorun-output').mkdir(parents=True)
        out.mkdir()
        (out / 'STOP').write_text('')  # the running export's own
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={})))  # an old run's
        os.utime(str(out / 'autorun-result.json'), (time.time() - 3600, time.time() - 3600))
        (installed / 'autorun.alive').write_text(json.dumps(dict(time=time.time(), busy='button')))
        (installed / 'FusionBulkExport.manifest').write_text(json.dumps(dict(version='99.0')))
        with contextlib.redirect_stdout(io.StringIO()):
            launcher.install(installed, out, 'export')
        self.assertTrue((out / 'STOP').exists())
        self.assertTrue((out / 'autorun-result.json').exists())
        (installed / 'autorun-output' / 'autorun-result.json').write_text(json.dumps(dict(
            busy=True, errors=[dict(stage='busy', error='An export started from the toolbar button is running')])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)
        lines = stream.getvalue().splitlines()  # no "Waiting for" before a prompt answer
        self.assertEqual(lines, ['Fusion is busy: An export started from the toolbar button is running'])

    def test_status_says_nothing_of_linked_copies_and_an_account_change_fails_the_command(self):
        out = self.root / 'out'
        out.mkdir()
        entries = [dict(kind='design', status='done', has_references=True, linked_occurrences=1, files=[
            dict(path='a v1.f3d', role='archive', bytes=1), dict(path='a v1.step', role='step', bytes=1)]),
            dict(kind='design', status='done', has_references=True, files=[dict(path='b v1.f3d', bytes=1),
                                                                          dict(path='b v1.step', bytes=1)])]
        (out / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=[], entries=entries)), encoding='utf-8')
        result = dict(mode='export', counts=dict(exported=2, linked_copies=1), stopped_code='account_changed',
                      stopped_reason='changed', errors=[])
        (out / 'autorun-result.json').write_text(json.dumps(result), encoding='utf-8')
        lines = launcher.summary(out)
        self.assertFalse(any('linked parts included' in line or 'copies with linked parts' in line for line in lines))
        self.assertIn(launcher.ACCOUNT_CHANGED_LINE, lines)
        installed = self.root / 'installed'
        installed.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            status = launcher.wait(installed, out, 86400, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, 1)

    def test_no_linked_copies_flag_any_more(self):
        out = self.root / 'out'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            launcher.main(['export', str(out), '--no-linked-copies'])

    def test_the_linked_copies_experiment_is_forwarded_in_the_job_only_when_set(self):
        installed, out = self.root / 'installed', self.root / 'out'
        installed.mkdir()
        job = lambda: json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))
        name = launcher.LINKED_COPIES_ENV
        self.assertEqual(name, 'FUSION_BULK_EXPORT_EXPERIMENT_LINKED_COPIES')
        with patch.object(launcher.shutil, 'copytree'):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(name, None)
                launcher.install(installed, out, 'export')
                self.assertNotIn('experiment_linked_copies', job())
            for value, sent in (('1', '1'), (' Innermost ', 'innermost'), ('compute', 'compute'), ('0', None),
                                ('yes', None), ('', None)):
                with patch.dict(os.environ, {name: value}):
                    launcher.install(installed, out, 'export')
                    self.assertEqual(job().get('experiment_linked_copies'), sent, value)
        # Every value the launcher passes on is one the add-in accepts, with the same meaning.
        from fusion_bulk_export import core
        self.assertEqual(core.LINKED_COPIES_ENV, name)
        self.assertEqual(sorted(launcher.LINKED_COPIES_VALUES), sorted(core.LINK_RECIPES))

    def test_no_step_flag_reaches_the_job_and_status_says_so(self):
        installed, out = self.root / 'installed', self.root / 'out'
        installed.mkdir()
        job = lambda: json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))
        with patch.object(launcher.shutil, 'copytree'):
            launcher.install(installed, out, 'export', step=False)
            self.assertIs(job()['step'], False)
            launcher.install(installed, out, 'export')
            self.assertNotIn('step', job())
        seen = []
        with patch.object(launcher, 'start', side_effect=lambda args: seen.append(args.no_step) or 0):
            launcher.main(['export', str(out), '--no-step'])
            launcher.main(['export', str(out)])
        self.assertEqual(seen, [True, False])
        jobs = []
        with patch.object(launcher, 'install', side_effect=lambda *a: jobs.append(a)), \
                patch.object(launcher, 'alive', return_value=True), patch.object(launcher, 'wait', return_value=0):
            args = NS(command='export', project=None, limit=None, folder=None, designs_only=False, no_step=True,
                      timeout=10)
            launcher.run_job(args, 'Darwin', out, installed)
        self.assertIs(jobs[0][7], False)
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts=dict(exported=1), step=False,
                                                                 errors=[])), encoding='utf-8')
        self.assertIn('STEP files were not requested for this export.', launcher.summary(out))
        from fusion_bulk_export import core
        self.assertEqual(launcher.STEP_OFF_NOTE, core.STEP_OFF_NOTE)

    def test_older_versions_flag_reaches_the_job_and_status_and_check_count_them(self):
        installed, out = self.root / 'installed', self.root / 'out'
        installed.mkdir()
        job = lambda: json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))
        with patch.object(launcher.shutil, 'copytree'):
            launcher.install(installed, out, 'export', older_versions='delete')
            self.assertEqual(job()['older_versions'], 'delete')
            launcher.install(installed, out, 'export', older_versions='keep')
            self.assertEqual(job()['older_versions'], 'keep')
            launcher.install(installed, out, 'export')
            self.assertNotIn('older_versions', job())  # the add-in's default, move
        seen = []
        with patch.object(launcher, 'start', side_effect=lambda args: seen.append(args.older_versions) or 0):
            for choice in ('move', 'keep', 'delete'):
                launcher.main(['export', str(out), '--older-versions', choice])
            launcher.main(['export', str(out)])
        self.assertEqual(seen, ['move', 'keep', 'delete', 'move'])
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            launcher.main(['export', str(out), '--older-versions', 'trash'])
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            launcher.main(['export', str(out), '--keep-latest-only'])  # replaced, never released
        jobs = []
        with patch.object(launcher, 'install', side_effect=lambda *a: jobs.append(a)), \
                patch.object(launcher, 'alive', return_value=True), patch.object(launcher, 'wait', return_value=0):
            args = NS(command='export', project=None, limit=None, folder=None, designs_only=False, no_step=False,
                      older_versions='delete', timeout=10)
            launcher.run_job(args, 'Darwin', out, installed)
        self.assertEqual(jobs[0][8], 'delete')
        entries = [dict(kind='design', status='removed', removed='2026-01-01T00:00:00+00:00', files=[],
                        removed_files=[dict(path='a v1.f3d'), dict(path='a v1.step')]),
                   dict(kind='design', status='moved', moved='2026-01-01T00:00:00+00:00', files=[],
                        moved_files=[dict(path='a v2.f3d', moved_to='Older versions/a v2.f3d')]),
                   dict(kind='design', status='done', files=[dict(path='a v3.f3d', bytes=1)])]
        (out / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=[], entries=entries)), encoding='utf-8')
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', counts=dict(exported=1), removed=dict(versions=1, files=2, kept=1, errors=0),
            moved=dict(versions=1, files=1, kept=0, errors=1), errors=[])), encoding='utf-8')
        lines = launcher.summary(out)
        self.assertIn('Deleted 1 older version (2 files).', lines)
        self.assertIn('Moved 1 older version (1 file) to Older versions.', lines)
        self.assertIn('Kept: 1 older version changed since it was exported (not deleted)', lines)
        self.assertIn('Not moved: 1 older version (the next run tries again)', lines)
        self.assertIn('Older versions deleted from this folder: 1 (2 files)', lines)
        self.assertIn('Older versions moved to the Older versions folder: 1 (1 file)', lines)
        from fusion_bulk_export import names, report
        self.assertEqual(launcher.removed_text(3, 1), report.removed_text(3, 1))
        self.assertEqual(launcher.moved_text(3, 1), report.moved_text(3, 1))
        self.assertEqual(launcher.OLDER_FOLDER, names.OLDER_FOLDER)

    def test_designs_only_flag_reaches_the_job(self):
        installed, out = self.root / 'installed', self.root / 'out'
        installed.mkdir()
        with patch.object(launcher.shutil, 'copytree'):
            launcher.install(installed, out, 'export', designs_only=True)
            self.assertTrue(json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))['designs_only'])
            launcher.install(installed, out, 'export')
            self.assertNotIn('designs_only', json.loads((installed / 'autorun.json').read_text(encoding='utf-8')))
        seen = []
        with patch.object(launcher, 'start', side_effect=lambda args: seen.append(args.designs_only) or 0):
            launcher.main(['export', str(out), '--designs-only'])
            launcher.main(['export', str(out)])
        self.assertEqual(seen, [True, False])

    def test_drawings_flag_is_accepted_and_only_says_drawing_export_was_removed(self):
        installed = self.root / 'AddIns' / 'FusionBulkExport'
        args = launcher.argparse.Namespace(output=str(self.root / 'out'), command='export', project=None,
                                           limit=None, folder=None, designs_only=False, drawings=True, timeout=10)
        stream, jobs = io.StringIO(), []
        with patch.object(launcher, 'system', return_value='Darwin'), \
                patch.object(launcher, 'addins_dir', return_value=installed.parent), \
                patch.object(launcher, 'alive', return_value=True), \
                patch.object(launcher, 'wait', side_effect=lambda *a, **k: jobs.append(
                    json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))) or 0), \
                contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.start(args), 0)
        lines = [line for line in stream.getvalue().splitlines() if 'drawing' in line.lower()]
        self.assertEqual(lines, [launcher.DRAWINGS_REMOVED])
        self.assertIn('can crash Fusion', lines[0])
        self.assertNotIn('drawings', jobs[0])
        seen = []
        with patch.object(launcher, 'start', side_effect=lambda args: seen.append(args.drawings) or 0):
            launcher.main(['export', str(self.root / 'out'), '--drawings'])
        self.assertEqual(seen, [True])
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            launcher.main(['export', '--help'])
        self.assertNotIn('--drawings', help_text.getvalue())

    def test_limit_rule_is_in_the_help(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit):
            launcher.main(['export', '--help'])
        text = ' '.join(stream.getvalue().split())
        self.assertIn('Other files are still saved in every folder visited up to then', text)

    def test_folder_arguments(self):
        self.assertEqual(launcher.folder_map(['p1:Parts/Brackets/', 'p1:Jigs', 'p2:/X']),
                         {'p1': ['Parts/Brackets', 'Jigs'], 'p2': ['X']})
        self.assertIsNone(launcher.folder_map(None))
        self.assertEqual(launcher.folder_map(['p1:id=urn:x/y', 'p1:Jigs']), {'p1': [dict(id='urn:x/y'), 'Jigs']})
        for bad in ('noseparator', 'p1:', ':Parts', 'p1:/', 'p1:id=', ':id=x'):
            with self.assertRaises(SystemExit):
                launcher.folder_map([bad])

    def test_an_interrupted_command_withdraws_its_job(self):
        installed = self.root / 'AddIns' / 'FusionBulkExport'
        args = launcher.argparse.Namespace(output=str(self.root / 'out'), command='export', project=None,
                                           limit=None, folder=None, designs_only=False, timeout=10)

        def consumed(installed, *rest, **kwargs):
            (installed / 'autorun.json').replace(installed / 'autorun.json.last')  # as the add-in does
            return 0
        for waiting in (KeyboardInterrupt, SystemExit('Timed out')):
            with patch.object(launcher, 'system', return_value='Darwin'), \
                    patch.object(launcher, 'addins_dir', return_value=installed.parent), \
                    patch.object(launcher, 'fusion_running', return_value=False), \
                    patch.object(launcher, 'launch_fusion'), \
                    patch.object(launcher, 'wait', side_effect=waiting), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stream:
                if waiting is KeyboardInterrupt:
                    # Ctrl-C: the "withdrawn" line and nothing after it (no traceback), status 130.
                    self.assertEqual(launcher.start(args), launcher.INTERRUPTED)
                    self.assertEqual(stream.getvalue(), 'The job was not started by Fusion and has been withdrawn.\n')
                else:
                    with self.assertRaises(SystemExit):
                        launcher.start(args)
            self.assertFalse((installed / 'autorun.json').exists())
            self.assertIn('withdrawn', stream.getvalue())
        # Ctrl-C after Fusion took the job: one line saying it goes on.
        with patch.object(launcher, 'system', return_value='Darwin'), \
                patch.object(launcher, 'addins_dir', return_value=installed.parent), \
                patch.object(launcher, 'alive', return_value=True), \
                patch.object(launcher, 'wait', side_effect=lambda *a, **k: consumed(installed) and None
                             or (_ for _ in ()).throw(KeyboardInterrupt)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stream:
            self.assertEqual(launcher.start(args), launcher.INTERRUPTED)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('Stopped waiting; the export goes on in Fusion'))
        (installed / 'autorun.json.last').unlink()
        with patch.object(launcher, 'system', return_value='Darwin'), \
                patch.object(launcher, 'addins_dir', return_value=installed.parent), \
                patch.object(launcher, 'alive', return_value=True), \
                patch.object(launcher, 'wait', side_effect=consumed):
            self.assertEqual(launcher.start(args), 0)
        self.assertTrue((installed / 'autorun.json.last').exists())

    def test_status_counts_items_still_only_in_the_journal(self):
        out = self.root / 'out'
        out.mkdir()
        done = dict(key='["h","p","a",1]', kind='design', status='done', id='a', files=[dict(path='x', bytes=5)])
        (out / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=[], entries=[done])))
        later = dict(done, key='["h","p","b",1]', id='b')
        (out / 'manifest.journal').write_text(json.dumps(dict(entry=later)) + '\n')
        text = '\n'.join(launcher.summary(out))
        self.assertIn('Designs: 2 entries {"done": 2}', text)
        self.assertIn('10 bytes', text)
        self.assertEqual(launcher.count_line(dict(exported=0, up_to_date=1, files=0, files_up_to_date=2, skipped=0,
                                                  failed=0)),
                         'Counts: 0 exported now, 1 already up to date, 0 other files saved, '
                         '2 other files already up to date, 0 skipped, 0 failed')

    def test_status_counts_slow_items_without_names(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Locked', status='done', files=[], slow_seconds=135),
            dict(name='Secret Quick', status='done', files=[])])))
        text = '\n'.join(launcher.summary(out))
        self.assertIn('Slow: 1 item took Fusion over a minute (the report lists it)', text)
        self.assertNotIn('Secret', text)

    def test_status_counts_unsupported_files_apart_from_failures(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Board', kind='file', status='unsupported', files=[],
                 error='Fusion cannot export this file type through its API (.fbrd)'),
            dict(name='Secret Old', kind='file', status='skipped', files=[],
                 error='Fusion cannot export this file type through its API (.fsch)')])))
        text = '\n'.join(launcher.summary(out))
        self.assertIn("Not supported: 2 files can't be exported by an add-in (drawings, electronics); they are not failures", text)
        self.assertNotIn('Secret', text)
        self.assertEqual(launcher.count_line(dict(exported=1, up_to_date=0, files=0, skipped=0, unsupported=6,
                                                  failed=0)),
                         "Counts: 1 exported now, 0 already up to date, 0 other files saved, 0 skipped, 0 failed; "
                         "6 files can't be exported by an add-in (drawings, electronics)")

    def test_run_time_files_come_from_the_installer_list(self):
        source = Path(launcher.__file__).resolve().parents[1] / 'FusionBulkExport'
        names = ['errors.log', 'autorun.json', '__pycache__', 'core.pyc', '.DS_Store', 'trace.log',
                 'autorun.py', 'FusionBulkExport.py', 'resources']
        self.assertEqual(launcher.runtime_files(str(source), names),
                         {'errors.log', 'autorun.json', '__pycache__', 'core.pyc', '.DS_Store', 'trace.log'})

    def test_wait_times_out(self):
        ticks = iter(range(0, 10000, 100))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            launcher.wait(self.root, self.root, 250, sleep=lambda _: None, clock=lambda: next(ticks))


if __name__ == '__main__':
    unittest.main()


class CanceledResultTests(unittest.TestCase):
    def test_summary_says_canceled_from_either_name(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            for result in (dict(canceled=True, cancelled=True), dict(cancelled=True), dict(canceled=True)):
                (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={}, **result)))
                lines = launcher.summary(out)
                self.assertIn('canceled: True', lines)
                self.assertFalse([line for line in lines if line.startswith('cancelled')])


class AddInVersionTests(unittest.TestCase):
    """The launcher installs its checkout's add-in only when none is installed or the installed
    one is older; it never downgrades, and replaces nothing while an export runs. (A tester's
    older checkout once downgraded the installed add-in, and the next toolbar click failed with
    an ImportError.)"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installed = self.root / 'AddIns' / 'FusionBulkExport'
        self.installer = launcher._installer()

    def addin(self, folder, version, body='# entry\n'):
        (folder / 'fusion_bulk_export').mkdir(parents=True, exist_ok=True)
        (folder / 'FusionBulkExport.manifest').write_text(json.dumps(dict(version=version)), encoding='utf-8')
        (folder / 'FusionBulkExport.py').write_text(body, encoding='utf-8')
        (folder / 'fusion_bulk_export' / '__init__.py').write_text('', encoding='utf-8')
        return folder

    def installed_copy(self, version, body='# entry\n', stamped=True):
        self.addin(self.installed, version, body)
        if stamped:
            (self.installed / self.installer.STAMP).write_text(self.installer.stamp_text(self.installed))

    def update(self, source):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            copied = launcher.update_addin(self.installed, source)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)  # one line says which add-in is used
        return copied, lines[0]

    def test_missing_add_in_is_installed_with_its_stamp(self):
        source = self.addin(self.root / 'checkout', '0.3.0')
        copied, line = self.update(source)
        self.assertTrue(copied)
        self.assertIn('Installing the add-in 0.3.0', line)
        self.assertEqual(self.installer.read_stamp(self.installed), self.installer.stamp(source))

    def test_an_older_installed_add_in_is_updated(self):
        self.installed_copy('0.2.3', '# old\n')
        copied, line = self.update(self.addin(self.root / 'checkout', '0.3.0'))
        self.assertTrue(copied)
        self.assertEqual(line, 'Updating the installed add-in 0.2.3 to 0.3.0 from this checkout.')
        self.assertEqual((self.installed / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# entry\n')

    def test_a_newer_installed_add_in_is_never_downgraded(self):
        self.installed_copy('0.3.0', '# newer\n')
        copied, line = self.update(self.addin(self.root / 'checkout', '0.2.3'))
        self.assertFalse(copied)
        self.assertIn('newer than this checkout (0.2.3) and is never downgraded', line)
        self.assertEqual((self.installed / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# newer\n')

    def test_the_same_version_is_kept_whether_or_not_its_files_match(self):
        source = self.addin(self.root / 'checkout', '0.3.0')
        same = (False, 'Using the installed add-in 0.3.0 (the same as this checkout).')
        self.installed_copy('0.3.0')
        self.assertEqual(self.update(source), same)
        # The same files without a stamp (installed by hand), or with an old one: still the same.
        self.installed_copy('0.3.0', stamped=False)
        self.assertEqual(self.update(source), same)
        (self.installed / self.installer.STAMP).write_text(json.dumps(dict(version='0.2.3', sha256='0' * 64)))
        self.assertEqual(self.update(source), same)
        # Another build of the same version (a test build; an older checkout before a version
        # bump): which is newer cannot be told, so the installed one stays.
        for stamped in (True, False):
            self.installed_copy('0.3.0', '# test build\n', stamped=stamped)
            copied, line = self.update(source)
            self.assertFalse(copied)
            self.assertEqual(line, 'Using the installed add-in 0.3.0: installed copy differs from this checkout; kept '
                                   'the installed one; run scripts/install.py to replace it.')
            self.assertEqual((self.installed / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# test build\n')

    def test_an_installed_copy_with_a_stale_version_label_is_named_as_such(self):
        # A Windows install over 0.2.3 copied the 0.3.1 files but kept 0.2.3's manifest and stamp
        # (same size, same date in the zip, so robocopy skipped them): the files are this
        # checkout's, only the label is old. Said so, and the label corrected.
        source = self.addin(self.root / 'checkout', '0.3.1')
        self.addin(self.installed, '0.2.3')
        (self.installed / self.installer.STAMP).write_text(json.dumps(dict(version='0.2.3', sha256='1' * 64)))
        copied, line = self.update(source)
        self.assertTrue(copied)
        self.assertEqual(line, 'The installed add-in has the files of this checkout (0.3.1) but an out-of-date '
                               'version label (0.2.3); correcting the label.')
        self.assertEqual(self.installer.manifest_version(self.installed), '0.3.1')
        self.assertEqual(self.installer.read_stamp(self.installed), self.installer.stamp(source))
        self.assertEqual(self.update(source), (False, 'Using the installed add-in 0.3.1 (the same as this checkout).'))

    def test_nothing_is_replaced_while_an_export_runs(self):
        self.installed_copy('0.2.3', '# old\n')
        (self.installed / 'autorun.alive').write_text(json.dumps(dict(time=time.time(), busy='button')))
        copied, line = self.update(self.addin(self.root / 'checkout', '0.3.0'))
        self.assertFalse(copied)
        self.assertIn('An export is running in Fusion', line)
        self.assertEqual((self.installed / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# old\n')
        # Idle (or an add-in from before the heartbeat said so): the update goes ahead.
        for heartbeat in (json.dumps(dict(time=time.time(), busy=None)), str(time.time())):
            (self.installed / 'autorun.alive').write_text(heartbeat)
            self.assertFalse(launcher.exporting(self.installed))
        self.assertTrue(self.update(self.root / 'checkout')[0])

    def test_a_job_from_an_older_checkout_keeps_the_newer_add_in(self):
        self.installed_copy('9.0.0', '# newer\n')
        out = self.root / 'out'
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            launcher.install(self.installed, out, 'export')
        self.assertIn('never downgraded', stream.getvalue())
        self.assertEqual((self.installed / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# newer\n')
        self.assertTrue((self.installed / 'autorun.json').is_file())  # the job is still sent
