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

spec = importlib.util.spec_from_file_location('autorun_launcher', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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

    def test_output_path_limit(self):
        with self.assertRaises(SystemExit):
            launcher.check_output('/' + 'x' * 120)

    def test_status_reports_counts_without_names(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', finished='t', fusion_version='v', counts=dict(exported=1, up_to_date=3, skipped=0, failed=1), stopped_reason='Secret folder gone',
            stopped_code='output_missing',
            found=2, bytes=10, errors=[dict(stage='x', error='Secret Design failed')])))
        (out / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(name='Secret Design', status='done', files=[dict(bytes=4), dict(bytes=6)]),
            dict(name='Hidden Part', status='failed', files=[])])))
        (out / 'autorun.log').write_text('t1 design 2/2\nt2 could not read ["Private Hub"]: no access\n'
                                         't3 autorun: Traceback ... Hidden Part\n')
        text = '\n'.join(launcher.summary(out))
        for secret in ('Secret', 'Hidden', 'Private'):
            self.assertNotIn(secret, text)
        self.assertIn('design 2/2', text)
        self.assertIn('"done": 1', text)
        self.assertIn('Counts: 1 exported now, 3 already up to date, 0 skipped, 1 failed', text)
        self.assertIn('Stopped by itself (output_missing)', text)
        self.assertIn('10 bytes', text)

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

    def test_install_writes_config_without_watch_marker(self):
        installed, out = self.root / 'AddIns' / 'FusionBulkExport', self.root / 'out'
        out.mkdir()
        (out / 'STOP').write_text('')
        installed.mkdir(parents=True)
        (installed / 'autorun.watch').write_text('')  # from an earlier version
        launcher.install(installed, out, 'export', ['p1'], 5)
        self.assertEqual(json.loads((installed / 'autorun.json').read_text()),
                         dict(output=str(out), mode='export', projects=['p1'], limit=5))
        self.assertFalse((installed / 'autorun.watch').exists())
        self.assertTrue((installed / 'FusionBulkExport.py').exists())
        # Every module of the add-in package is installed (a broad "autorun*" ignore once dropped one).
        source = Path(launcher.__file__).resolve().parents[1] / 'FusionBulkExport' / 'fusion_bulk_export'
        for module in source.glob('*.py'):
            self.assertTrue((installed / 'fusion_bulk_export' / module.name).is_file(), module.name)
        self.assertFalse((out / 'STOP').exists())
        launcher.set_startup(installed, True)
        self.assertTrue(json.loads((installed / 'FusionBulkExport.manifest').read_text())['runOnStartup'])

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

    def test_reset_clears_stale_job_files_and_keeps_heartbeat(self):
        installed = self.root / 'FusionBulkExport'
        installed.mkdir()
        for name in ('autorun.watch', 'autorun.json', 'autorun.alive'):
            (installed / name).write_text('')
        (installed / 'FusionBulkExport.manifest').write_text(json.dumps(dict(runOnStartup=True)))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launcher.reset(installed), 0)
        self.assertEqual(sorted(p.name for p in installed.iterdir()), ['FusionBulkExport.manifest', 'autorun.alive'])
        self.assertFalse(json.loads((installed / 'FusionBulkExport.manifest').read_text())['runOnStartup'])
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

    def test_folder_arguments(self):
        self.assertEqual(launcher.folder_map(['p1:Parts/Brackets/', 'p1:Jigs', 'p2:/X']),
                         {'p1': ['Parts/Brackets', 'Jigs'], 'p2': ['X']})
        self.assertIsNone(launcher.folder_map(None))
        for bad in ('noseparator', 'p1:', ':Parts', 'p1:/'):
            with self.assertRaises(SystemExit):
                launcher.folder_map([bad])

    def test_wait_times_out(self):
        ticks = iter(range(0, 10000, 100))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            launcher.wait(self.root, self.root, 250, sleep=lambda _: None, clock=lambda: next(ticks))


if __name__ == '__main__':
    unittest.main()
