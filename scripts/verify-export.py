#!/usr/bin/env python3
"""Check an export folder: every finished item is there unchanged (size and SHA-256), every
archive opens with every member readable, every STEP is complete.

Usage: verify-export.py OUTPUT_FOLDER [--names] [--strict]

A STEP with no bodies, or with fewer solids than the distinct bodies Fusion showed,
is a NOTE (--strict makes it a problem); other differences in the count are only
counted (solids_noted), since a STEP writer may write a part used twice once or twice. Prints counts only, unless --names is given (manifests hold
private design names). Exit status 0 means every check passed.

The checks live in the add-in package (fusion_bulk_export/checks.py) and need no Fusion; this
script runs them from a checkout of the repository. `autorun.py check OUTPUT_FOLDER` does the same.
"""
import json
from pathlib import Path
import sys

PACKAGE_DIR = Path(__file__).resolve().parent.parent / 'FusionBulkExport'
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from fusion_bulk_export import checks  # noqa: E402  (found through the path above)


def printer(names):
    def show(number, entry, line, problems, notes):
        label = entry.get('name') if names else ('file %d' if entry.get('kind') == 'file' else 'design %d') % number
        if line.startswith('status '):
            print('%s: %s (%s)' % (label, line, entry.get('error') if names else 'see manifest'))
            return
        print('%s: %s' % (label, line))
        for problem in problems:
            print('  PROBLEM ' + problem)
        for note in notes:
            print('  NOTE ' + note)
    return show


def main(argv):
    if not argv or argv[0].startswith('-'):
        raise SystemExit(__doc__)
    root = Path(argv[0]).resolve()
    totals = checks.check_folder(root, strict='--strict' in argv, show=printer('--names' in argv))
    print(json.dumps(totals))
    return 0 if totals['done'] and not totals['failed_checks'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
