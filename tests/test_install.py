"""Installing into Fusion's AddIns folder: scripts/install.py and the two no-Python launchers in the zip."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load('install')
package = load('package')
NEXT_STEP = ('Installed. In Fusion press Shift+S and find FusionBulkExport in the list: type its name in '
             'Search, or check Add-ins under Type on the left. Check its Run on Startup box first, then '
             'switch Run on. The window closes when you switch it on; that is normal.')


def fake_addin(folder):
    """A small add-in folder with source files and every kind of run-time file."""
    folder.mkdir(parents=True)
    (folder / 'FusionBulkExport.manifest').write_text('{"version": "9.9.9"}', encoding='utf-8')
    (folder / 'FusionBulkExport.py').write_text('# entry\n', encoding='utf-8')
    (folder / 'fusion_bulk_export').mkdir()
    (folder / 'fusion_bulk_export' / 'core.py').write_text('# core\n', encoding='utf-8')
    (folder / 'fusion_bulk_export' / '__pycache__').mkdir()
    (folder / 'fusion_bulk_export' / '__pycache__' / 'core.cpython-312.pyc').write_bytes(b'x')
    (folder / 'autorun-output').mkdir()
    (folder / 'autorun-output' / 'autorun-result.json').write_text('{}', encoding='utf-8')
    for name in ('autorun.json', 'autorun.alive', 'errors.log', 'startup-error.txt', 'jobs-error.txt', 'trace'):
        (folder / name).write_text('run time', encoding='utf-8')
    return folder


def tree(folder):
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


class AddinsDirTests(unittest.TestCase):
    def test_macos(self):
        self.assertEqual(install.addins_dir('Darwin', {'HOME': '/Users/someone'}),
                         Path('/Users/someone/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns'))

    def test_windows(self):
        self.assertEqual(install.addins_dir('Windows', {'APPDATA': 'C:/Users/someone/AppData/Roaming'}),
                         Path('C:/Users/someone/AppData/Roaming') / 'Autodesk' / 'Autodesk Fusion 360' / 'API'
                         / 'AddIns')

    def test_windows_without_appdata_and_other_systems_refuse(self):
        with self.assertRaises(SystemExit):
            install.addins_dir('Windows', {})
        with self.assertRaises(SystemExit):
            install.addins_dir('Linux', {'HOME': '/home/someone'})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = fake_addin(self.base / 'src' / 'FusionBulkExport')
        self.license = self.base / 'LICENSE'
        self.license.write_text('license', encoding='utf-8')
        self.target = self.base / 'AddIns'

    def tearDown(self):
        self.temp.cleanup()

    def test_copies_source_and_license_but_no_run_time_files(self):
        installed = install.install(self.target, self.source, self.license)
        self.assertEqual(installed, self.target / 'FusionBulkExport')
        self.assertEqual(tree(installed), ['FusionBulkExport.manifest', 'FusionBulkExport.py', 'LICENSE',
                                           'fusion_bulk_export/core.py', 'install-stamp.json'])
        self.assertEqual(os.listdir(str(self.target)), ['FusionBulkExport'])

    def test_replaces_an_older_copy_as_a_whole(self):
        old = self.target / 'FusionBulkExport'
        (old / 'fusion_bulk_export').mkdir(parents=True)
        (old / 'fusion_bulk_export' / 'removed_module.py').write_text('# gone in the new version', encoding='utf-8')
        (old / 'FusionBulkExport.py').write_text('# old entry', encoding='utf-8')
        (old / 'errors.log').write_text('old log', encoding='utf-8')
        (self.target / 'OtherAddIn').mkdir()
        install.install(self.target, self.source, self.license)
        self.assertEqual(tree(old), ['FusionBulkExport.manifest', 'FusionBulkExport.py', 'LICENSE',
                                     'fusion_bulk_export/core.py', 'install-stamp.json'])
        self.assertEqual((old / 'FusionBulkExport.py').read_text(encoding='utf-8'), '# entry\n')
        self.assertTrue((self.target / 'OtherAddIn').is_dir())

    def test_the_stamp_names_the_version_and_a_hash_of_the_files(self):
        installed = install.install(self.target, self.source, self.license)
        stamp = install.read_stamp(installed)
        self.assertEqual(stamp, dict(version='9.9.9', sha256=install.content_hash(self.source)))
        # The run-time files and the stamp itself are not part of the hash; any shipped byte is.
        (self.source / 'errors.log').write_text('later', encoding='utf-8')
        self.assertEqual(install.content_hash(self.source), stamp['sha256'])
        self.assertEqual(install.content_hash(installed), stamp['sha256'])
        # CRLF reads as LF. Bytes, not write_text: text mode on Windows would turn the \n into \r\n.
        (self.source / 'FusionBulkExport.py').write_bytes(b'# entry\r\n')
        self.assertEqual(install.content_hash(self.source), stamp['sha256'])
        (self.source / 'FusionBulkExport.py').write_text('# changed\n', encoding='utf-8')
        self.assertNotEqual(install.content_hash(self.source), stamp['sha256'])
        self.assertLess(install.version_key('0.2.3'), install.version_key('0.3'))
        self.assertLess(install.version_key('0.9.1'), install.version_key('0.10.0'))

    def test_refuses_a_source_without_manifest(self):
        (self.source / 'FusionBulkExport.manifest').unlink()
        with self.assertRaises(SystemExit):
            install.install(self.target, self.source, self.license)
        self.assertFalse((self.target / 'FusionBulkExport').exists())

    @unittest.skipIf(platform.system() == 'Windows', 'symlinks need extra rights on Windows')
    def test_replaces_a_developer_symlink_without_touching_its_target(self):
        checkout = fake_addin(self.base / 'checkout' / 'FusionBulkExport')
        self.target.mkdir()
        (self.target / 'FusionBulkExport').symlink_to(checkout, target_is_directory=True)
        install.install(self.target, self.source, self.license)
        self.assertFalse((self.target / 'FusionBulkExport').is_symlink())
        self.assertTrue((checkout / 'errors.log').exists())

    def test_uninstall(self):
        install.install(self.target, self.source, self.license)
        self.assertEqual(install.uninstall(self.target), self.target / 'FusionBulkExport')
        self.assertFalse((self.target / 'FusionBulkExport').exists())
        self.assertIsNone(install.uninstall(self.target))

    def test_uninstall_leaves_a_folder_that_is_not_the_add_in(self):
        (self.target / 'FusionBulkExport').mkdir(parents=True)
        (self.target / 'FusionBulkExport' / 'something.txt').write_text('x', encoding='utf-8')
        with self.assertRaises(SystemExit):
            install.uninstall(self.target)
        self.assertTrue((self.target / 'FusionBulkExport' / 'something.txt').exists())

    def test_main_installs_this_checkout_into_the_platform_folder(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(install.main([], system='Darwin', environ={'HOME': str(self.base)}), 0)
        installed = (self.base / 'Library' / 'Application Support' / 'Autodesk' / 'Autodesk Fusion 360' / 'API'
                     / 'AddIns' / 'FusionBulkExport')
        self.assertIn(NEXT_STEP, output.getvalue())
        expected = sorted([relative.as_posix() for _, relative in install.addin_files(ROOT / 'FusionBulkExport')]
                          + ['LICENSE', 'install-stamp.json'])
        self.assertEqual(tree(installed), expected)
        with contextlib.redirect_stdout(io.StringIO()):
            install.main(['--uninstall'], system='Windows', environ={'APPDATA': str(self.base)})
            install.main(['--uninstall', '--target', str(installed.parent)])
        self.assertFalse(installed.exists())


class LauncherTests(unittest.TestCase):
    """Run the launchers from a real release zip, extracted the way the user gets it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        target = package.build(self.base / 'dist')
        with zipfile.ZipFile(str(target)) as archive:
            archive.extractall(str(self.base / 'unzipped'))
        self.folder = self.base / 'unzipped' / ('FusionBulkExport-%s' % package.version())
        self.home = self.base / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def expected(self):
        return tree(self.folder / 'FusionBulkExport')

    @unittest.skipIf(platform.system() == 'Windows' or not shutil.which('bash'), 'needs bash')
    def test_mac_launcher_installs_and_replaces(self):
        script = self.folder / 'Install (macOS).command'
        addins = self.home / 'Library' / 'Application Support' / 'Autodesk' / 'Autodesk Fusion 360' / 'API' / 'AddIns'
        (addins / 'FusionBulkExport').mkdir(parents=True)
        (addins / 'FusionBulkExport' / 'stale.py').write_text('old', encoding='utf-8')
        for _ in range(2):
            result = subprocess.run(['bash', str(script)], stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, env=dict(os.environ, HOME=str(self.home)), timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(NEXT_STEP, result.stdout)
        self.assertEqual(tree(addins / 'FusionBulkExport'), self.expected())
        self.assertEqual(sorted(os.listdir(str(addins))), ['FusionBulkExport'])

    def test_the_zip_carries_the_stamp_install_py_writes(self):
        shipped = install.read_stamp(self.folder / 'FusionBulkExport')
        self.assertEqual(shipped, install.stamp(ROOT / 'FusionBulkExport'))
        self.assertEqual(install.content_hash(self.folder / 'FusionBulkExport'), shipped['sha256'])

    @unittest.skipIf(platform.system() == 'Windows' or not shutil.which('bash'), 'needs bash')
    def test_mac_launcher_refuses_without_the_add_in_folder(self):
        shutil.rmtree(str(self.folder / 'FusionBulkExport'))
        result = subprocess.run(['bash', str(self.folder / 'Install (macOS).command')], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, env=dict(os.environ, HOME=str(self.home)),
                                timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Cannot find the FusionBulkExport folder', result.stdout)
        self.assertEqual(os.listdir(str(self.home)), [])

    @unittest.skipUnless(platform.system() == 'Windows', 'needs Windows')
    def test_windows_launcher_installs(self):
        appdata = self.home / 'AppData'
        appdata.mkdir()
        result = subprocess.run(['cmd', '/c', str(self.folder / 'Install (Windows).bat')], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, env=dict(os.environ, APPDATA=str(appdata)),
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(NEXT_STEP, result.stdout)
        installed = appdata / 'Autodesk' / 'Autodesk Fusion 360' / 'API' / 'AddIns' / 'FusionBulkExport'
        self.assertEqual(tree(installed), self.expected())


if __name__ == '__main__':
    unittest.main()
