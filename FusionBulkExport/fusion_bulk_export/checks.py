"""Checks of exported files, shared by the add-in and scripts/verify-export.py. Pure stdlib (no
adsk), so they also run outside Fusion.

- step_facts(): is a STEP file complete (header, end marker), and how many solids it holds.
- archive_facts(): does an .f3d (or .f3z) open as a zip with Fusion's members; with full=True,
  every member is also read to the end, which checks its CRC.
- inline(): the quick checks made right after each export. A STEP or archive that is broken
  raises BadExport (the design is tried again); a STEP with no bodies, or fewer solids than the
  distinct bodies Fusion showed, is only a warning stored on the manifest entry (see
  solid_warning(); other differences are only recorded).
- check_folder(): "Check this folder": every finished item of an export folder checked again,
  with full hashes. Used by `autorun.py check` and `verify-export.py`; it needs no Fusion.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

from .journal import Journal, replay
from .names import long_path
from .words import plural

SOLID = re.compile(rb'=\s*(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(')
CHUNK = 1 << 20
OVERLAP = 256          # bytes kept between chunks, so a solid split across two is still found
HEAD = 4096
SMALL_STEP = 2048      # a STEP this small rarely holds anything


class BadExport(Exception):
    """An exported file that is broken (not just unusual)."""


def step_facts(path):
    """(problems, notes, solids) for a STEP file, read in chunks (STEP files can be large).
    problems: the file is incomplete. notes: unusual, not broken."""
    problems, notes = [], []
    solids = 0
    head = b''
    tail = b''
    carry = b''
    with open(long_path(path), 'rb') as stream:
        while True:
            chunk = stream.read(CHUNK)
            final = not chunk
            buffer = carry + chunk
            if len(head) < HEAD:
                head = (head + chunk)[:HEAD]
            cut = len(buffer) if final else max(len(buffer) - OVERLAP, 0)
            solids += sum(1 for match in SOLID.finditer(buffer) if match.start() < cut)
            carry = buffer[cut:]
            tail = (tail + chunk)[-OVERLAP:]
            if final:
                break
    size = os.stat(long_path(path)).st_size
    if not head.lstrip().startswith(b'ISO-10303-21;'):
        problems.append('STEP header missing')
    if not tail.rstrip().endswith(b'END-ISO-10303-21;'):
        problems.append('STEP truncated (no END-ISO-10303-21)')
    if b'FILE_SCHEMA' not in head:
        notes.append('STEP FILE_SCHEMA missing')
    if size < SMALL_STEP:
        notes.append('STEP suspiciously small (%d bytes)' % size)
    return problems, notes, solids


def archive_facts(path, full=True):
    """(problems, member designs, members not CRC-checked) for a Fusion archive."""
    path = Path(path)
    problems = []
    unchecked = 0
    try:
        with zipfile.ZipFile(long_path(path)) as archive:
            names = archive.namelist()
            if full:
                for info in archive.infolist():
                    try:
                        with archive.open(info) as stream:
                            while stream.read(CHUNK):
                                pass  # reading to the end checks the CRC
                    except NotImplementedError:
                        # Fusion stores some members with Zstandard (method 93); Python reads it from 3.14.
                        unchecked += 1
                    except (zipfile.BadZipFile, OSError, EOFError) as error:
                        problems.append('corrupt archive member %s: %s' % (info.filename, error))
    except zipfile.BadZipFile as error:
        return ['archive is not a zip: ' + str(error)], 0, 0
    designs = [n for n in names if n.lower().endswith('.f3d')]
    if path.suffix.lower() == '.f3z':
        if not designs:
            problems.append('.f3z contains no .f3d')
        if 'Manifest.json' not in names:
            problems.append('.f3z has no Manifest.json')
    elif 'Manifest.dat' not in names or not any('FusionAssetName' in n for n in names):
        problems.append('.f3d lacks Manifest.dat or FusionAssetName members')
    return problems, len(designs), unchecked


def solid_warning(solids, expected, distinct=None):
    """The warning for a STEP's solid count against Fusion's visible counts, or None.

    expected counts each shown occurrence (two instances of a part are two solids); distinct
    counts each shown component once. A STEP writer may write either, or something in between
    (seen on real Fusion: about twice Fusion's count, and other in-between numbers), so a
    difference alone says nothing: the numbers are only recorded (step_solids beside Fusion's
    counts). A warning is given only when the STEP is empty (with the bodies Fusion showed, if
    any) or holds fewer solids than the distinct count: bodies are then really missing."""
    if solids == 0:
        return 'STEP has no bodies' + (' (Fusion showed %d)' % expected if expected else '')
    if distinct is not None and solids < distinct:
        return 'STEP has %d solids, fewer than the %d distinct ones Fusion showed' % (solids, distinct)
    return None


def solids_agree(solids, expected, distinct=None):
    """Whether a STEP's solid count is one of Fusion's counts exactly."""
    return solids == expected or (distinct is not None and solids == distinct)


def is_step(path):
    return str(path).lower().endswith(('.step', '.stp'))


def is_archive(path):
    return str(path).lower().endswith(('.f3d', '.f3z'))


def file_role(info):
    """What a recorded file of a manifest entry is: its 'role' ('archive', 'step',
    'archive_with_linked_parts', 'thumbnail', 'file'), or, for entries written before roles
    existed (which never held a copy with linked parts), what its extension says."""
    role = (info or {}).get('role')
    if role:
        return role
    path = str((info or {}).get('path') or '')
    if is_archive(path):
        return 'archive'
    if is_step(path):
        return 'step'
    if path.lower().endswith('.png'):
        return 'thumbnail'
    return 'file'


def inline(paths, expected=None, distinct=None):
    """Quick checks right after a design's export: {'step_solids': n, 'warnings': [...]}.
    Raises BadExport when a file is broken."""
    facts = dict(step_solids=None, warnings=[])
    for path in paths:
        if is_archive(path):
            problems, _, _ = archive_facts(path, full=False)
            broken = [p for p in problems if p.startswith('archive is not a zip')]
            if broken:
                raise BadExport('The exported archive is damaged: ' + broken[0])
            facts['warnings'] += problems
        elif is_step(path):
            problems, notes, solids = step_facts(path)
            if problems:
                raise BadExport('The exported STEP file is incomplete: ' + '; '.join(problems))
            facts['step_solids'] = solids
            warning = solid_warning(solids, expected, distinct)
            if warning:
                facts['warnings'].append(warning)
    return facts


def file_sha256(path):
    sha = hashlib.sha256()
    with open(long_path(path), 'rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            sha.update(chunk)
    return sha.hexdigest()


def read_manifest(root):
    """manifest.json of an export folder with its journal (items finished after the last full
    save) applied."""
    root = Path(root)
    with open(long_path(root / 'manifest.json'), encoding='utf-8') as stream:
        manifest = json.loads(stream.read())
    manifest.setdefault('entries', [])
    pending = Journal(root / 'manifest.journal').read()
    if pending:
        replay(manifest, pending)
    return manifest


NEW_TOTALS = ('entries', 'done', 'designs', 'other_files', 'not_designs', 'skipped', 'unsupported', 'failed',
              'step_ok', 'solids_match', 'solids_noted', 'solids_differ', 'solids_unknown', 'warnings', 'missing', 'changed',
              'failed_checks', 'members_not_crc_checked', 'linked_copies', 'step_off', 'step_failed')


def check_folder(root, strict=False, show=None):
    """Check every finished item in an export folder again: each file is there with its recorded
    size and SHA-256, each archive opens with every member readable, each STEP is complete and
    its solid count is compared with Fusion's.

    show(number, entry, line, problems, notes), when given, is called per item (the command line
    prints it). Returns a dict of counts; 'failed_checks' is the number of items with a problem
    (0 means the folder checks out). strict makes a solid-count difference a problem."""
    root = Path(root)
    manifest = read_manifest(root)
    totals = dict((key, 0) for key in NEW_TOTALS)
    for number, entry in enumerate(manifest['entries'], 1):
        totals['entries'] += 1
        status = entry.get('status')
        if status != 'done':
            # A skip is recorded with its reason (not a design, or a file Fusion cannot hand
            # out): nothing to check. Anything else has not been exported yet.
            if status == 'unsupported':
                totals['unsupported'] += 1  # a type Fusion cannot export: nothing to check
                continue
            if status == 'skipped':
                totals['skipped'] += 1
                if str(entry.get('error') or '').startswith('Not a Fusion design'):
                    totals['not_designs'] += 1
                    continue
            else:
                totals['failed'] += 1
                totals['failed_checks'] += 1
            if show:
                show(number, entry, 'status %s' % status, [], [])
            continue
        totals['done'] += 1
        problems, notes = [], []
        files = entry.get('files') or []
        for info in files:
            path = root / info.get('path', '')
            if not os.path.isfile(long_path(path)):
                problems.append('file missing: ' + info.get('path', ''))
                totals['missing'] += 1
                continue
            size = os.stat(long_path(path)).st_size
            if ('bytes' in info and size != info['bytes']) or ('sha256' in info and file_sha256(path) != info['sha256']):
                problems.append('file changed since export: ' + info.get('path', ''))
                totals['changed'] += 1
        line = ''
        if entry.get('kind') == 'file':
            totals['other_files'] += 1
            line = 'other file, ' + plural(len(files), 'file')
        else:
            totals['designs'] += 1
            archive = next((root / f['path'] for f in files if file_role(f) == 'archive'), None)
            step = next((root / f['path'] for f in files if file_role(f) == 'step'), None)
            for linked in [root / f['path'] for f in files if file_role(f) == 'archive_with_linked_parts']:
                totals['linked_copies'] += 1
                if os.path.isfile(long_path(linked)):
                    linked_problems, _, unchecked = archive_facts(linked)
                    problems += ['copy with linked parts: ' + problem for problem in linked_problems]
                    totals['members_not_crc_checked'] += unchecked
            # No STEP by intent: not requested for that run ("off"), or Fusion would not write it
            # ("failed": a warning, tried again on the next export). Not a problem here.
            no_step = entry.get('step') in ('off', 'failed')
            if no_step and archive is None:
                problems.append('archive not recorded')
            elif no_step:
                suffix = archive.suffix.lower().lstrip('.')
                totals[suffix] = totals.get(suffix, 0) + 1
                members = 0
                if os.path.isfile(long_path(archive)):
                    archive_problems, members, unchecked = archive_facts(archive)
                    problems += archive_problems
                    totals['members_not_crc_checked'] += unchecked
                totals['step_off' if entry.get('step') == 'off' else 'step_failed'] += 1
                line = '%s, %s, %s' % (archive.suffix, plural(members, 'member design'),
                                       'STEP not requested' if entry.get('step') == 'off'
                                       else 'STEP not written (Fusion could not write it)')
            elif archive is None or step is None:
                problems.append('archive or STEP not recorded')
            else:
                suffix = archive.suffix.lower().lstrip('.')
                totals[suffix] = totals.get(suffix, 0) + 1
                members = solids = 0
                if os.path.isfile(long_path(archive)):
                    archive_problems, members, unchecked = archive_facts(archive)
                    problems += archive_problems
                    totals['members_not_crc_checked'] += unchecked
                if os.path.isfile(long_path(step)):
                    step_problems, step_notes, solids = step_facts(step)
                    problems += step_problems
                    notes += step_notes
                    if not step_problems:
                        totals['step_ok'] += 1
                    # STEP leaves out hidden bodies, so compare with the visible count when recorded.
                    expected = entry.get('visible_solid_bodies', entry.get('solid_bodies'))
                    if expected is None:
                        totals['solids_unknown'] += 1
                    else:
                        distinct = entry.get('visible_solid_bodies_distinct')
                        warning = solid_warning(solids, expected, distinct)
                        if warning is None or (solids == 0 and expected == 0):
                            # A count that differs without a warning is only noted in the totals.
                            totals['solids_match' if solids_agree(solids, expected, distinct)
                                   or solids == 0 else 'solids_noted'] += 1
                        else:
                            # Advisory: the STEP translator can drop bodies it cannot write. The .f3d
                            # is the complete copy.
                            totals['solids_differ'] += 1
                            (problems if strict else notes).append(warning)
                    line = '%s, %s, STEP %d bytes, %s%s%s' % (
                        archive.suffix, plural(members, 'member design'), os.stat(long_path(step)).st_size,
                        plural(solids, 'solid'),
                        '' if expected is None else ' (Fusion: %d)' % expected,
                        ', and an experimental copy with linked parts' if any(file_role(f) == 'archive_with_linked_parts'
                                                                             for f in files) else '')
        if entry.get('warnings'):
            totals['warnings'] += 1
        if show:
            show(number, entry, line, problems, notes)
        if problems:
            totals['failed_checks'] += 1
    return totals
