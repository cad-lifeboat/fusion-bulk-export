#!/usr/bin/env python3
"""Build dist/FusionBulkExport-<version>.zip: the add-in folder ready to add in Fusion.

The zip holds one folder, FusionBulkExport/, with the add-in, its manifest, icons and
the license. Run-time files and caches are left out.
"""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / 'FusionBulkExport'
SKIP_DIRS = {'__pycache__', 'autorun-output', 'selftest-output'}
SKIP_FILES = {'autorun.json', 'autorun.json.last', 'autorun.watch', 'autorun.alive',
              'selftest.json', 'selftest.json.last', 'startup-error.txt', 'errors.log', 'trace', 'trace.log'}


def files():
    for path in sorted(ADDIN.rglob('*')):
        relative = path.relative_to(ADDIN)
        if path.is_file() and not SKIP_DIRS & set(relative.parts) and path.name not in SKIP_FILES \
                and path.suffix not in ('.pyc', '.pyo'):
            yield path, relative


def build(out_dir=ROOT / 'dist'):
    version = json.loads((ADDIN / 'FusionBulkExport.manifest').read_text(encoding='utf-8'))['version']
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ('FusionBulkExport-%s.zip' % version)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files():
            info = zipfile.ZipInfo(str(Path('FusionBulkExport') / relative), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo('FusionBulkExport/LICENSE', date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, (ROOT / 'LICENSE').read_bytes())
    return target


if __name__ == '__main__':
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'dist'))
