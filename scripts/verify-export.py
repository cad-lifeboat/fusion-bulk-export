#!/usr/bin/env python3
"""Check an export folder: every done entry has a readable archive and a valid STEP.

Usage: verify-export.py OUTPUT_FOLDER [--names] [--strict]

A STEP whose solid count differs from Fusion's visible solid count is a NOTE
(--strict makes it a problem). Prints counts only, unless --names is given (manifests hold private design names).
When selftest-result.json is present, STEP solid counts are compared with the
solid-body counts Fusion reported for each design. Exit status 0 means every
check passed.
"""
import json
from pathlib import Path
import re
import sys
import zipfile

UNCHECKED = [0]  # archive members whose compression this Python cannot read
SOLID = re.compile(rb'=\s*(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(')


def check_archive(path):
    problems = []
    unchecked = 0
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for info in archive.infolist():
                try:
                    with archive.open(info) as stream:
                        while stream.read(1 << 20):
                            pass  # reading to the end checks the CRC
                except NotImplementedError:
                    # Fusion stores some members with Zstandard (method 93); Python reads it from 3.14.
                    unchecked += 1
                except (zipfile.BadZipFile, OSError, EOFError) as error:
                    problems.append('corrupt archive member %s: %s' % (info.filename, error))
    except zipfile.BadZipFile as error:
        return ['archive is not a zip: ' + str(error)], 0
    if unchecked:
        UNCHECKED[0] += unchecked
    designs = [n for n in names if n.lower().endswith('.f3d')]
    if path.suffix.lower() == '.f3z':
        if not designs:
            problems.append('.f3z contains no .f3d')
        if 'Manifest.json' not in names:
            problems.append('.f3z has no Manifest.json')
    elif 'Manifest.dat' not in names or not any('FusionAssetName' in n for n in names):
        problems.append('.f3d lacks Manifest.dat or FusionAssetName members')
    return problems, len(designs)


def check_step(path):
    data = path.read_bytes()
    problems = []
    if not data.lstrip().startswith(b'ISO-10303-21;'):
        problems.append('STEP header missing')
    if b'FILE_SCHEMA' not in data[:4096]:
        problems.append('STEP FILE_SCHEMA missing')
    if not data.rstrip().endswith(b'END-ISO-10303-21;'):
        problems.append('STEP truncated (no END-ISO-10303-21)')
    if len(data) < 2048:
        problems.append('STEP suspiciously small (%d bytes)' % len(data))
    return problems, len(SOLID.findall(data))


def main(argv):
    if not argv or argv[0].startswith('-'):
        raise SystemExit(__doc__)
    UNCHECKED[0] = 0
    root = Path(argv[0]).resolve()
    names = '--names' in argv
    strict = '--strict' in argv
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    observed = {}
    result_path = root / 'selftest-result.json'
    if result_path.is_file():
        for design in json.loads(result_path.read_text(encoding='utf-8'))['designs']:
            observed[design['id']] = design.get('solid_bodies')
    totals = dict(entries=0, done=0, not_designs=0, f3d=0, step_ok=0, solids_match=0, solids_differ=0,
                  solids_unknown=0, failed_checks=0)
    for number, entry in enumerate(manifest['entries'], 1):
        totals['entries'] += 1
        label = entry['name'] if names else 'design %d' % number
        if entry['status'] != 'done':
            if entry['status'] == 'skipped' and str(entry.get('error', '')).startswith('Not a Fusion design'):
                totals['not_designs'] += 1  # drawings and other files are expected to be skipped
                continue
            print('%s: status %s (%s)' % (label, entry['status'], entry.get('error') if names else 'see manifest'))
            totals['failed_checks'] += 1
            continue
        totals['done'] += 1
        problems, notes = [], []
        archive, step = (root / entry['files'][0]['path'], root / entry['files'][1]['path'])
        totals[archive.suffix.lower().lstrip('.')] = totals.get(archive.suffix.lower().lstrip('.'), 0) + 1
        archive_problems, members = check_archive(archive)
        problems += archive_problems
        step_problems, solids = check_step(step)
        problems += step_problems
        if not step_problems:
            totals['step_ok'] += 1
        # STEP leaves out hidden bodies, so compare with the visible count when recorded.
        expected = entry.get('visible_solid_bodies', entry.get('solid_bodies', observed.get(entry['id'])))
        if expected is None:
            totals['solids_unknown'] += 1
        elif expected == solids:
            totals['solids_match'] += 1
        else:
            # Advisory: the visible count is approximate for linked components, and the STEP
            # translator can drop bodies it cannot write. The .f3d is the complete copy.
            totals['solids_differ'] += 1
            (problems if strict else notes).append('STEP has %d solids, Fusion reported %d' % (solids, expected))
        print('%s: %s, %d member design(s), STEP %d bytes, %d solid(s)%s' % (
            label, archive.suffix, members, step.stat().st_size, solids,
            '' if expected is None else ' (Fusion: %d)' % expected))
        for problem in problems:
            print('  PROBLEM ' + problem)
        for note in notes:
            print('  NOTE ' + note)
        if problems:
            totals['failed_checks'] += 1
    totals['members_not_crc_checked'] = UNCHECKED[0]
    print(json.dumps(totals))
    return 0 if totals['done'] and not totals['failed_checks'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
