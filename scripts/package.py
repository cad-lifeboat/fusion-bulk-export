#!/usr/bin/env python3
"""Build dist/FusionBulkExport-<version>.zip, the file people download.

Layout:
  FusionBulkExport-<version>/README.txt                 the three steps (packaging/README.txt)
  FusionBulkExport-<version>/Install (macOS).command    copies the add-in into Fusion (executable)
  FusionBulkExport-<version>/Install (Windows).bat      the same for Windows (CRLF line endings)
  FusionBulkExport-<version>/FusionBulkExport/          the add-in, its manifest, icons and LICENSE

Run-time files and caches are left out (the list lives in install.py).
"""
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install  # noqa: E402  (scripts/install.py: the shared list of run-time files)

ROOT = Path(__file__).resolve().parents[1]
NAME = install.NAME
DATE = (2020, 1, 1, 0, 0, 0)
MAC_INSTALLER = 'Install (macOS).command'
WINDOWS_INSTALLER = 'Install (Windows).bat'


def version(root=ROOT):
    manifest = root / NAME / (NAME + '.manifest')
    return json.loads(manifest.read_text(encoding='utf-8'))['version']


def files(root=ROOT):
    """(path, relative path) of every add-in file that goes in the zip."""
    return install.addin_files(root / NAME)


def _text(path, newline):
    text = path.read_text(encoding='utf-8').replace('\r\n', '\n')
    return text.replace('\n', newline).encode('utf-8')


def _add(archive, name, data, mode=0o644):
    info = zipfile.ZipInfo(name, date_time=DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix, so unzip tools honour the permission bits below
    info.external_attr = (0o100000 | mode) << 16
    archive.writestr(info, data)


def build(out_dir=ROOT / 'dist', root=ROOT):
    root = Path(root)
    current = version(root)
    top = '%s-%s' % (NAME, current)
    packaging = root / 'packaging'
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ('%s.zip' % top)
    readme = _text(packaging / 'README.txt', '\r\n').replace(b'{version}', current.encode('ascii'))
    with zipfile.ZipFile(str(target), 'w', zipfile.ZIP_DEFLATED) as archive:
        _add(archive, top + '/README.txt', readme)
        _add(archive, '%s/%s' % (top, MAC_INSTALLER), _text(packaging / MAC_INSTALLER, '\n'), 0o755)
        _add(archive, '%s/%s' % (top, WINDOWS_INSTALLER), _text(packaging / WINDOWS_INSTALLER, '\r\n'))
        for path, relative in files(root):
            _add(archive, '%s/%s/%s' % (top, NAME, relative.as_posix()), path.read_bytes())
        _add(archive, '%s/%s/LICENSE' % (top, NAME), (root / 'LICENSE').read_bytes())
    return target


if __name__ == '__main__':
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'dist'))
