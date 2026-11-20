"""Release zip: versioned top folder with README.txt, the two installers and the add-in; nothing that
writes to the user's Fusion account."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package', ROOT / 'scripts' / 'package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)

ENTRY_FILE = 'FusionBulkExport/FusionBulkExport.py'
# Calls that would upload, create, save or delete data in the user's Fusion account.
FORBIDDEN = [
    ('uploadFile', re.compile(r'uploadFile')),
    ('dataProjects.add', re.compile(r'dataProjects\s*\.\s*add\b')),
    ('dataFolders.add', re.compile(r'dataFolders\s*\.\s*add\b')),
    ('saveAs(', re.compile(r'\.saveAs\s*\(')),
    # Document.save(description) always takes an argument; the manifest's own save() takes none.
    ('.save(...)', re.compile(r'\.save\s*\(\s*[^)\s]')),
    ('deleteMe', re.compile(r'deleteMe')),
]
# The only deleteMe allowed: removing the add-in's own toolbar button and command on stop.
UI_DELETE = re.compile(r'^\s*_?(control|definition)\.deleteMe\(\)\s*$')


def forbidden_calls(name, text):
    """[(line number, what)] for every forbidden call in one file of the zip."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for what, pattern in FORBIDDEN:
            if not pattern.search(line):
                continue
            if what == 'deleteMe' and name.endswith('/' + ENTRY_FILE) and UI_DELETE.match(line):
                continue
            found.append((number, what))
    return found


def build():
    temp = tempfile.TemporaryDirectory()
    target = package.build(Path(temp.name))
    archive = zipfile.ZipFile(str(target))
    archive.temp = temp
    return target, archive


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target, cls.archive = build()
        cls.version = package.version()
        cls.top = 'FusionBulkExport-%s/' % cls.version

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()
        cls.archive.temp.cleanup()

    def test_zip_name_and_top_folder(self):
        self.assertEqual(self.target.name, 'FusionBulkExport-%s.zip' % self.version)
        names = self.archive.namelist()
        self.assertTrue(all(name.startswith(self.top) for name in names), names)

    def test_zip_holds_readme_installers_and_add_in(self):
        names = self.archive.namelist()
        for required in ('README.txt', 'Install (macOS).command', 'Install (Windows).bat'):
            self.assertIn(self.top + required, names)
        for required in ('FusionBulkExport.py', 'FusionBulkExport.manifest', 'LICENSE', 'resources/32x32.png',
                         'fusion_bulk_export/core.py', 'fusion_bulk_export/autorun.py', 'ui/tree.js'):
            self.assertIn(self.top + 'FusionBulkExport/' + required, names)
        top_level = {name[len(self.top):].split('/')[0] for name in names}
        self.assertEqual(top_level, {'README.txt', 'Install (macOS).command', 'Install (Windows).bat',
                                     'FusionBulkExport'})

    def test_no_run_time_files_or_caches(self):
        for name in self.archive.namelist():
            self.assertNotIn('__pycache__', name)
            self.assertFalse(name.endswith(('.pyc', '.pyo', '.DS_Store', 'errors.log')), name)
            self.assertNotIn('autorun.json', name)
            self.assertNotIn('autorun-output', name)

    def test_mac_installer_is_executable_with_unix_line_endings(self):
        info = self.archive.getinfo(self.top + 'Install (macOS).command')
        self.assertEqual((info.external_attr >> 16) & 0o777, 0o755)
        data = self.archive.read(info)
        self.assertTrue(data.startswith(b'#!/bin/bash\n'))
        self.assertNotIn(b'\r', data)

    def test_windows_installer_has_crlf_line_endings(self):
        data = self.archive.read(self.top + 'Install (Windows).bat')
        self.assertIn(b'\r\n', data)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        self.assertIn(b'robocopy', data)
        self.assertTrue(data.rstrip().endswith(b'exit /b %RESULT%'))

    def test_other_files_are_not_executable(self):
        for info in self.archive.infolist():
            if not info.filename.endswith('.command'):
                self.assertEqual((info.external_attr >> 16) & 0o777, 0o644, info.filename)

    def test_readme_names_the_version_steps_and_help(self):
        text = self.archive.read(self.top + 'README.txt').decode('utf-8')
        self.assertIn('Fusion Bulk Export %s' % self.version, text)
        self.assertNotIn('{version}', text)
        for needed in ('1. Install', '2. Switch it on in Fusion', '3. Export', 'Big account',
                       'https://github.com/cad-lifeboat/fusion-bulk-export', 'Not affiliated with Autodesk',
                       'green "+"'):
            self.assertIn(needed, text)
        self.assertEqual(text.count('\n'), text.count('\r\n'))

    def text_files(self):
        for info in self.archive.infolist():
            if info.filename.endswith(('.py', '.js', '.html', '.command', '.bat', '.manifest', '.txt', '.json')):
                yield info.filename, self.archive.read(info).decode('utf-8', 'replace')

    def violations(self):
        return {name: found for name, text in self.text_files() for found in [forbidden_calls(name, text)]
                if found}

    def test_nothing_in_the_zip_can_change_the_fusion_account(self):
        self.assertEqual(self.violations(), {})


class ForbiddenCallTests(unittest.TestCase):
    def test_catches_each_forbidden_call(self):
        name = 'FusionBulkExport-1.0.0/FusionBulkExport/fusion_bulk_export/core.py'
        for line, what in [('folder.uploadFile(path)', 'uploadFile'),
                           ('hub.dataProjects.add("x")', 'dataProjects.add'),
                           ('project.rootFolder.dataFolders.add("x")', 'dataFolders.add'),
                           ('document.saveAs("n", folder, "", "")', 'saveAs('),
                           ('document.save("description")', '.save(...)'),
                           ('data_file.deleteMe()', 'deleteMe'),
                           ('_control.deleteMe()', 'deleteMe')]:
            self.assertEqual(forbidden_calls(name, line), [(1, what)], line)

    def test_allows_manifest_save_and_ui_cleanup_in_the_entry_file(self):
        self.assertEqual(forbidden_calls('x/FusionBulkExport/fusion_bulk_export/core.py', '        self.save()'), [])
        entry = 'FusionBulkExport-1.0.0/' + ENTRY_FILE
        self.assertEqual(forbidden_calls(entry, '            _control.deleteMe()\n    _definition.deleteMe()'), [])
        self.assertEqual(forbidden_calls(entry, 'design.deleteMe()'), [(1, 'deleteMe')])


if __name__ == '__main__':
    unittest.main()
