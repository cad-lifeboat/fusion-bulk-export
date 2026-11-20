#!/usr/bin/env python3
"""Install Fusion Bulk Export into Fusion's add-ins folder (macOS or Windows).

  install.py                 copy this checkout's add-in into Fusion's AddIns folder
  install.py --uninstall     remove it again
  install.py --target DIR    use another AddIns folder (for testing)

An older copy is replaced as a whole. Run-time files (job files, logs, caches) are
never copied. Fusion lists the add-in by itself afterwards: press Shift+S, open the
Add-Ins tab, switch FusionBulkExport on and tick Run on Startup.

The release zip carries "Install (macOS).command" and "Install (Windows).bat", which
do the same for people without Python.
"""
import argparse
import os
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
NAME = 'FusionBulkExport'
SOURCE = ROOT / NAME
MANIFEST = NAME + '.manifest'
NEXT_STEP = ('Installed. In Fusion press Shift+S, open the Add-Ins tab, switch FusionBulkExport on '
             'and tick Run on Startup.')

# Files and folders the add-in creates at run time, plus caches. Never installed or packaged.
RUNTIME_DIRS = ('__pycache__', 'autorun-output')
RUNTIME_FILES = ('autorun.json', 'autorun.json.last', 'autorun.alive', 'startup-error.txt', 'errors.log',
                 'trace', 'trace.log', '.DS_Store')
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
