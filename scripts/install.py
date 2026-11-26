#!/usr/bin/env python3
"""Install Fusion Bulk Export into Fusion's add-ins folder (macOS or Windows).

  install.py                 copy this checkout's add-in into Fusion's AddIns folder
  install.py --uninstall     remove it again
  install.py --target DIR    use another AddIns folder (for testing)

An older copy is replaced as a whole. Run-time files (job files, logs, caches) are
never copied. The installed copy gets install-stamp.json: its version and a hash of
its files (the release zip carries the same stamp), so the command-line launcher can
tell whether it is older than a checkout. Fusion lists the add-in by itself afterward: press Shift+S,
find FusionBulkExport in the list (Search, or check Add-ins under Type), check its Run on Startup box
first, then switch Run on. The window closes when you switch it on; that is normal.

The release zip carries "Install (macOS).command" and "Install (Windows).bat", which
do the same for people without Python.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
NAME = 'FusionBulkExport'
SOURCE = ROOT / NAME
MANIFEST = NAME + '.manifest'
NEXT_STEP = ('Installed. In Fusion press Shift+S and find FusionBulkExport in the list: type its name in '
             'Search, or check Add-ins under Type on the left. Check its Run on Startup box first, then '
             'switch Run on. The window closes when you switch it on; that is normal.')

# Files and folders the add-in creates at run time, plus caches. Never installed or packaged.
RUNTIME_DIRS = ('__pycache__', 'autorun-output')
# The stamp an installation carries (see stamp()). Never copied from a source folder: it is
# written for exactly the files installed.
STAMP = 'install-stamp.json'
RUNTIME_FILES = ('autorun.json', 'autorun.json.last', 'autorun.alive', 'startup-error.txt', 'jobs-error.txt',
                 'errors.log', 'trace', 'trace.log', '.DS_Store', STAMP)
RUNTIME_SUFFIXES = ('.pyc', '.pyo')


def is_runtime(relative):
    """True for a path (relative to the add-in folder) that must not be shipped."""
    parts = Path(relative).parts
    if any(part in RUNTIME_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in RUNTIME_DIRS or name in RUNTIME_FILES or name.endswith(RUNTIME_SUFFIXES)


def addin_files(source=SOURCE):
    """(path, relative path) of every file to ship, sorted."""
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_file() and not is_runtime(relative):
            yield path, relative


def content_hash(source=SOURCE, leave_out=()):
    """SHA-256 over the add-in's shipped files: each relative path and its bytes, in order, with
    CRLF read as LF (so a Windows checkout and the zip agree). The LICENSE the installers add
    beside the add-in is not part of it, so a checkout and its installation agree; nor are the
    relative paths in leave_out (MANIFEST: are these the same files whatever their version label?)."""
    digest = hashlib.sha256()
    for path, relative in addin_files(source):
        if relative.as_posix() == 'LICENSE' or relative.as_posix() in leave_out:
            continue
        digest.update(relative.as_posix().encode('utf-8') + b'\0')
        digest.update(path.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return digest.hexdigest()


def manifest_version(folder):
    """The version in folder's manifest, or None (no manifest, or unreadable)."""
    try:
        value = json.loads((Path(folder) / MANIFEST).read_text(encoding='utf-8')).get('version')
        return str(value) if value else None
    except (OSError, ValueError, AttributeError):
        return None


def stamp(source=SOURCE):
    """What install-stamp.json holds for these files: {"version", "sha256"}."""
    return dict(version=manifest_version(source), sha256=content_hash(source))


def stamp_text(source=SOURCE):
    return json.dumps(stamp(source), indent=2, sort_keys=True) + '\n'


def read_stamp(folder):
    """An installation's install-stamp.json, or None (installed before stamps, or unreadable)."""
    try:
        value = json.loads((Path(folder) / STAMP).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def version_key(version):
    """A version for comparing: its numbers ("0.3.1" < "0.10"); None stays None."""
    if not version:
        return None
    import re
    return tuple(int(number) for number in re.findall(r'\d+', str(version)))


def addins_dir(system=None, environ=None):
    """Fusion's per-user AddIns folder."""
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    if system == 'Windows':
        if not environ.get('APPDATA'):
            raise SystemExit('APPDATA is not set; cannot find Fusion\'s add-ins folder.')
        base = Path(environ['APPDATA'])
    elif system == 'Darwin':
        home = environ.get('HOME') or str(Path.home())
        base = Path(home) / 'Library' / 'Application Support'
    else:
        raise SystemExit('Fusion runs only on macOS and Windows; use --target to choose a folder.')
    return base / 'Autodesk' / 'Autodesk Fusion 360' / 'API' / 'AddIns'


def _remove(path):
    """Remove an installed copy; a symlink (a developer's link to a checkout) is unlinked, not followed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(str(path))


def install(target, source=SOURCE, license_file=ROOT / 'LICENSE'):
    """Copy the add-in into target/FusionBulkExport, replacing an older copy. Returns the installed path."""
    source = Path(source)
    if not (source / MANIFEST).is_file():
        raise SystemExit('Cannot find the add-in at %s (no %s).' % (source, MANIFEST))
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    installed = target / NAME
    staging = target / ('.%s-installing' % NAME)
    _remove(staging)
    try:
        for path, relative in addin_files(source):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(destination))
        if Path(license_file).is_file() and not (staging / 'LICENSE').exists():
            shutil.copy2(str(license_file), str(staging / 'LICENSE'))
        (staging / STAMP).write_text(stamp_text(source), encoding='utf-8')
        _remove(installed)
        staging.rename(installed)
    except BaseException:
        _remove(staging)
        raise
    return installed


def uninstall(target):
    """Remove target/FusionBulkExport. Returns the removed path, or None if it was not installed."""
    installed = Path(target) / NAME
    if installed.is_symlink():
        installed.unlink()
        return installed
    if not installed.exists():
        return None
    if not (installed / MANIFEST).is_file():
        raise SystemExit('%s does not look like Fusion Bulk Export (no %s); left alone.' % (installed, MANIFEST))
    shutil.rmtree(str(installed))
    return installed


def main(argv=None, system=None, environ=None):
    parser = argparse.ArgumentParser(description='Install Fusion Bulk Export into Fusion\'s add-ins folder.')
    parser.add_argument('--uninstall', action='store_true', help='remove the installed add-in')
    parser.add_argument('--target', type=Path, help='AddIns folder (default: Fusion\'s, for this user)')
    args = parser.parse_args(argv)
    target = args.target or addins_dir(system, environ)
    if args.uninstall:
        removed = uninstall(target)
        print('Removed %s. Restart Fusion to unload it.' % removed if removed else 'Not installed in %s.' % target)
        return 0
    installed = install(target)
    print('Copied to %s' % installed)
    print(NEXT_STEP)
    return 0


if __name__ == '__main__':
    sys.exit(main())
