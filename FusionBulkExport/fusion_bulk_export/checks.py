"""Checks of exported files, shared by the add-in and scripts/verify-export.py. Pure stdlib (no
adsk), so they also run outside Fusion.

- step_facts(): is a STEP file complete (header, end marker), and how many solids it holds.
- archive_facts(): does an .f3d (or .f3z) open as a zip with Fusion's members; with full=True,
  every member is also read to the end, which checks its CRC.
- inline(): the quick checks made right after each export. A STEP or archive that is broken
  raises BadExport (the design is tried again); a STEP missing bodies (empty, fewer solids than
  expected, hidden bodies left out) is only a warning stored on the manifest entry, and a design
  with no solid bodies at all only a note (see step_verdict(); other differences are only
  recorded).
- check_folder(): "Check this folder": every finished item of an export folder checked again,
  with full hashes (older versions moved or deleted on purpose, status "moved" or "removed",
  are counted, not checked; CAM tool libraries, manifest["tool_libraries"], are checked by size
  and SHA-256). Used by `autorun.py check` and `verify-export.py`; it needs no Fusion.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

from .journal import Journal, replay
from .names import long_path
from .words import form, plural

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


def solid_warning(solids, expected, distinct=None, where='Fusion showed'):
    """The warning for a STEP's solid count against Fusion's counts, or None.

    expected counts each shown occurrence (two instances of a part are two solids); distinct
    counts each shown component once. A STEP writer may write either, or something in between
    (seen on real Fusion: about twice Fusion's count, and other in-between numbers), so a
    difference alone says nothing: the numbers are only recorded (step_solids beside Fusion's
    counts). A warning is given only when the STEP is empty (with the bodies Fusion showed, if
    any) or holds fewer solids than the distinct count: bodies are then really missing. where
    says what the numbers are ('Fusion showed'; 'in the design' when hidden bodies were asked
    for too)."""
    if solids == 0:
        if not expected:
            return 'STEP has no bodies'
        if where == 'Fusion showed':
            return 'STEP has no bodies (Fusion showed %d)' % expected
        return 'STEP has no bodies (%s %s)' % (plural(expected, 'solid body'), where)
    if distinct is not None and solids < distinct:
        return 'STEP has %s, fewer than the %d distinct ones %s' % (plural(solids, 'solid'), distinct, where)
    return None


def solids_agree(solids, expected, distinct=None):
    """Whether a STEP's solid count is one of Fusion's counts exactly."""
    return solids == expected or (distinct is not None and solids == distinct)


def no_solids_note(surfaces=None, meshes=None):
    """The quiet note for a design with no solid bodies at all (only sketches, surfaces or
    meshes): its STEP has no solids, which is expected and not a warning."""
    if surfaces is None or meshes is None:
        return 'No solid bodies (sketches or surfaces only): its STEP is empty.'
    if not surfaces and not meshes:
        return 'No solid bodies (sketches only): its STEP is empty.'
    kinds = ' and '.join(kind for kind, count in (('surfaces', surfaces), ('meshes', meshes)) if count)
    return 'No solid bodies (%s only): its STEP has no solids.' % kinds


def hidden_warning(hidden, empty):
    """The warning for hidden solid bodies the STEP left out (this Fusion could not include
    them); empty: the STEP has no bodies because every body is hidden."""
    if empty:
        if hidden == 1:
            return 'Its one body is hidden, so its STEP is empty; the body is in the .f3d.'
        return 'All its %d bodies are hidden, so its STEP is empty; they are in the .f3d.' % hidden
    return 'Its STEP leaves out %s; %s in the .f3d.' % (plural(hidden, 'hidden body'),
                                                         'it is' if hidden == 1 else 'they are')


# Every warning step_verdict() can give, old wordings included: what entry_step() replaces.
VERDICT_WARNING = re.compile(r'^(STEP has (no bodies|\d+ solids?, fewer than)|Its STEP leaves out \d+ hidden bod'
                             r'|Its one body is hidden, so its STEP|All its \d+ bodies are hidden, so its STEP)')


def step_verdict(solids, facts):
    """What a STEP's solid count means for a design, from the facts Fusion gave (a manifest
    entry, or the adapter's details): {'warnings': [...], 'note': text or None,
    'hidden_left_out': n, 'expected': the count compared with, or None when unknown}.

    - No solid bodies at all (solid_bodies 0): an empty STEP is expected; only a quiet note.
    - Hidden bodies included (step_includes_hidden true): every solid body of the design
      (solid_bodies, each component once) is expected.
    - Otherwise the STEP holds the visible bodies only: hidden ones it left out are one warning
      (they are in the .f3d), and fewer solids than the visible distinct ones another.
    The tolerance is solid_warning()'s: only an empty STEP or fewer solids than the distinct count
    is a warning."""
    facts = facts or {}
    result = dict(warnings=[], note=None, hidden_left_out=0, expected=None)
    total = facts.get('solid_bodies')
    shown = facts.get('visible_solid_bodies', total)
    distinct = facts.get('visible_solid_bodies_distinct')
    if total == 0 or (total is None and shown == 0):
        result['expected'] = 0
        if solids == 0:
            result['note'] = no_solids_note(facts.get('surface_bodies'), facts.get('mesh_bodies'))
        return result
    if facts.get('step_includes_hidden') is True and total is not None:
        result['expected'] = total
        warning = solid_warning(solids, total, total, where='in the design')
        if warning:
            result['warnings'].append(warning)
        return result
    result['expected'] = shown
    seen = distinct if distinct is not None else shown
    hidden = total - seen if total is not None and seen is not None and total > seen else 0
    if hidden:
        result['hidden_left_out'] = hidden
        empty = solids == 0 and not seen
        result['warnings'].append(hidden_warning(hidden, empty))
        if empty:
            return result
    warning = solid_warning(solids, shown, distinct)
    if warning:
        result['warnings'].append(warning)
    return result


def entry_step(entry):
    """An exported design's STEP warnings and note as this version words them:
    {'warnings': [...every warning of the entry], 'note': text or None, 'hidden_left_out': n}.
    The STEP's own warnings are worked out again from the recorded counts (step_verdict), so a
    folder written by an older version reads the same: a design with no solid bodies is no
    longer a warning, and hidden bodies are named as such."""
    entry = entry or {}
    warnings = [str(warning) for warning in entry.get('warnings') or []]
    solids = entry.get('step_solids')
    if solids is None or entry.get('step') in ('off', 'failed'):
        return dict(warnings=warnings, note=None, hidden_left_out=0)
    verdict = step_verdict(solids, entry)
    kept = [warning for warning in warnings if not VERDICT_WARNING.match(warning)]
    return dict(warnings=verdict['warnings'] + kept, note=verdict['note'],
                hidden_left_out=verdict['hidden_left_out'])


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


def inline(paths, counts=None):
    """Quick checks right after a design's export: {'step_solids': n, 'warnings': [...]}, plus
    'step_note' for a design with no solid bodies (see step_verdict(); counts are the adapter's
    facts). Raises BadExport when a file is broken."""
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
            verdict = step_verdict(solids, counts)
            facts['warnings'] += verdict['warnings']
            if verdict['note']:
                facts['step_note'] = verdict['note']
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


def design_identity(entry):
    """What tells one design from another across its versions: its cloud id (its key, or the
    entry itself, for an entry written without one)."""
    return str(entry.get('id') or entry.get('key') or id(entry))


def newest_designs(manifest):
    """The newest exported version of every design in the folder (status "done"), each once,
    in manifest order."""
    newest = {}
    for entry in manifest.get('entries') or []:
        if not isinstance(entry, dict) or (entry.get('kind') or 'design') != 'design' or entry.get('status') != 'done':
            continue
        identity = design_identity(entry)
        known = newest.get(identity)
        if known is None or (entry.get('version') or 0) >= (known.get('version') or 0):
            newest[identity] = entry
    return list(newest.values())


def archive_paths(manifest):
    """{design id: the relative path of its newest exported .f3d in this folder}."""
    archives = {}
    for entry in newest_designs(manifest):
        archive = next((info.get('path') for info in entry.get('files') or [] if file_role(info) == 'archive'), None)
        if archive and entry.get('id') is not None:
            archives[str(entry['id'])] = archive
    return archives


def inserted_designs(manifest):
    """The assemblies of an export folder: designs whose newest exported version records at least
    one design inserted from another design (its manifest "references"), each design once,
    whether exported in this run or an earlier one. [{'entry': the entry, 'inserted': [{'id',
    'name', 'path'}]}], by project, folder and name. path is the inserted design's newest
    exported .f3d here, or None when it is not in this export; worked out from the folder now,
    so a folder from an older version (references without paths, or paths of an earlier run)
    reads the same."""
    archives = archive_paths(manifest)
    found = []
    for entry in newest_designs(manifest):
        inserted, seen = [], set()
        for reference in entry.get('references') or []:
            if isinstance(reference, dict):
                identity, name = reference.get('id'), reference.get('name')
            elif isinstance(reference, str) and reference:
                identity, name = reference, None
            else:
                continue
            identity = None if identity is None else str(identity)
            if identity is not None and (identity in seen or identity == str(entry.get('id'))):
                continue
            seen.add(identity)
            inserted.append(dict(id=identity, name=None if name is None else str(name),
                                 path=archives.get(identity) if identity is not None else None))
        if inserted:
            found.append(dict(entry=entry, inserted=inserted))
    found.sort(key=lambda item: (str(item['entry'].get('project') or '').casefold(),
                                 '/'.join(item['entry'].get('path') or []).casefold(),
                                 str(item['entry'].get('name') or '').casefold()))
    return found


def inserted_note(assemblies):
    """The plain note about the assemblies of inserted_designs() (counts only, never names), or
    None when there are none: their .f3d holds only links to the designs inserted into them."""
    count = len(assemblies)
    if not count:
        return None
    its = 'its' if count == 1 else 'their'
    without_step = sum(1 for item in assemblies
                       if not any(file_role(info) == 'step' for info in item['entry'].get('files') or []))
    missing_ids, unnamed = set(), 0
    for item in assemblies:
        for inserted in item['inserted']:
            if inserted['path'] is None:
                if inserted['id'] is None:
                    unnamed += 1
                else:
                    missing_ids.add(inserted['id'])
    missing = len(missing_ids) + unnamed
    clauses = []
    if without_step < count:
        clauses.append('the full geometry is in %s .step%s' % (
            its, ' (%d without one)' % without_step if without_step else ''))
    if missing:
        clauses.append('the inserted designs are in this folder too, except %s not in this export'
                       % plural(missing, 'design'))
    else:
        clauses.append('every inserted design is in this folder too')
    return ('%s %s designs inserted from other designs. %s .f3d holds only links to them; %s. For a single file '
            'with everything, export the assembly as .f3z from File > Export in Fusion.'
            % (plural(count, 'assembly'), form(count, 'uses'), its.capitalize(), ', and '.join(clauses)))


NEW_TOTALS = ('entries', 'done', 'designs', 'other_files', 'not_designs', 'skipped', 'unsupported', 'failed',
              'step_ok', 'solids_match', 'solids_noted', 'solids_differ', 'solids_unknown', 'warnings', 'missing', 'changed',
              'failed_checks', 'members_not_crc_checked', 'linked_copies', 'step_off', 'step_failed',
              'hidden_left_out', 'no_solid_bodies', 'removed', 'moved', 'tool_libraries', 'tool_libraries_failed')


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
        if status in ('removed', 'moved'):
            # An older version whose files were deleted, or moved into the Older versions folder,
            # on purpose once its newest version was saved ("Older versions of a design"): not
            # where it was recorded, nothing to check, and not a problem.
            totals[status] += 1
            continue
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
                    verdict = step_verdict(solids, entry)
                    expected = verdict['expected']
                    shortfall = [w for w in verdict['warnings'] if w.startswith('STEP has')]
                    if verdict['hidden_left_out']:
                        # Expected of a Fusion that cannot include hidden bodies: a note, never a
                        # problem (the .f3d holds them).
                        totals['hidden_left_out'] += 1
                        notes += [w for w in verdict['warnings'] if not w.startswith('STEP has')]
                    if verdict['note']:
                        totals['no_solid_bodies'] += 1  # sketches or surfaces only: nothing to flag
                    elif expected is None:
                        totals['solids_unknown'] += 1
                    elif shortfall:
                        # Advisory: the STEP translator can drop bodies it cannot write. The .f3d
                        # is the complete copy.
                        totals['solids_differ'] += 1
                        (problems if strict else notes).extend(shortfall)
                    elif not verdict['hidden_left_out']:
                        # A count that differs without a warning is only noted in the totals.
                        distinct = (expected if entry.get('step_includes_hidden') is True
                                    else entry.get('visible_solid_bodies_distinct'))
                        totals['solids_match' if solids_agree(solids, expected, distinct)
                               or solids == 0 else 'solids_noted'] += 1
                    line = '%s, %s, STEP %d bytes, %s%s%s' % (
                        archive.suffix, plural(members, 'member design'), os.stat(long_path(step)).st_size,
                        plural(solids, 'solid'),
                        '' if expected is None else ' (Fusion: %d)' % expected,
                        ', and an experimental copy with linked parts' if any(file_role(f) == 'archive_with_linked_parts'
                                                                             for f in files) else '')
        if entry_step(entry)['warnings']:
            totals['warnings'] += 1
        if show:
            show(number, entry, line, problems, notes)
        if problems:
            totals['failed_checks'] += 1
    # The CAM tool libraries (toollibs.py): each recorded file there with its size and SHA-256. A
    # library that failed in its last export is counted as failed, as a design is; the file of its
    # last good export, when there is one, is checked all the same.
    from .toollibs import check_entry
    for number, entry in enumerate(manifest.get('tool_libraries') or [], 1):
        if not isinstance(entry, dict):
            continue
        problems, line = check_entry(root, entry)
        if entry.get('status') == 'failed':
            totals['tool_libraries_failed'] += 1
            totals['failed_checks'] += 1
            line = 'status failed' + ('' if not entry.get('path') else ', earlier file: ' + line)
        else:
            totals['tool_libraries'] += 1
        totals['missing'] += sum(1 for problem in problems if problem.startswith('file missing'))
        totals['changed'] += sum(1 for problem in problems if problem.startswith('file changed'))
        if show:
            show(number, entry, line, problems, [])
        if problems and entry.get('status') != 'failed':
            totals['failed_checks'] += 1
    return totals
