"""The human side of an export folder: index.html and index.csv, the end-of-run summary, and
opening the folder or report in the system's file browser.

index.html is self-contained (no scripts, no network), readable in light and dark mode and
works when opened straight from disk: every item with its status, links to its files (relative
to the folder) and, for failures (listed first), the reason and what to do next.
"""
import csv
import html
import io
import os
import subprocess
import sys
from urllib.parse import quote

from . import toollibs
from .checks import bundle_info, entry_step, file_role, inserted_designs, inserted_note
from .core import (ARCHIVE_NOT_REQUESTED, ARCHIVE_OFF, ARCHIVE_OFF_NOTE, DRAWING_REASON, LEGACY_NATIVE_REASON,
                   LINKS_UNREADABLE, LOW_MEMORY_NOTE, MOVED, OLDER_FOLDER, REMOVED, STEP_FAILED,
                   STEP_OFF, STEP_NOT_REQUESTED, STEP_OFF_NOTE, was_canceled)
from .names import long_path
from .words import form, plural

ORDER = {'failed': 0, 'unreadable': 1, 'skipped': 2, 'unsupported': 3, 'warning': 4, 'done': 5}
LABEL = {'failed': 'Failed', 'unreadable': 'Not read', 'skipped': 'Skipped', 'unsupported': 'Not supported',
         'done': 'Exported'}
# Files Fusion's API cannot export (electronics, drawings): neither failures nor left out by the
# user, so they are counted and named apart from both.
UNSUPPORTED_NEXT = 'To keep a copy, open it in Fusion and use File > Export.'
DRAWING_NEXT = 'To keep a copy, open the drawing in Fusion and use File > Export (PDF).'


def unsupported_text(count):
    """'7 files can't be exported by an add-in (drawings, electronics)'."""
    return "%s can't be exported by an add-in (drawings, electronics)" % plural(count, 'file')


def unsupported_note(count):
    """The summary's line for them: not failures, and where they are listed."""
    return '%s. %s not %s; the report lists %s.' % (
        unsupported_text(count), 'It is' if count == 1 else 'They are', form(count, 'a failure', 'failures'),
        form(count, 'it', 'them'))
LINKS_OUT_OF_DATE = ('Some of its links are out of date: a linked design has a newer version than this design '
                     'uses. The export holds the versions it uses; open it in Fusion and update its links '
                     'if you want the newer ones.')
# The file roles of a design (checks.file_role). The experimental copy with linked parts exists only
# in folders exported with the testing switch core.LINKED_COPIES_ENV on.
ROLE_LABEL = {'archive': 'F3D', 'step': 'STEP', 'archive_with_linked_parts': 'F3D with linked parts (experimental)',
              'f3z_bundle': 'F3Z'}
ACCOUNT_CHANGED_NOTE = ("This design's version or date in the Fusion account changed while it was exported "
                        "with its linked parts; the export stopped. Check its version history in Fusion.")


Y_UP = 'Y up'  # shown beside a design made Y-up (index.html); Z up, the usual, is not shown
STEP_RETRIED = 'Its STEP file is tried again on the next export into this folder.'
WARNING_STEP = ('The .f3d archive is the complete copy; open the design in Fusion if you need the STEP '
                'to hold every body.')


def next_step(status, error):
    """What the user can do about an item, in a few words."""
    text = (error or '').lower()
    if status == 'done':
        return ''
    if status == 'unsupported':
        return DRAWING_NEXT if 'drawing' in text else UNSUPPORTED_NEXT
    if status == 'skipped':
        if 'not a fusion design' in text:
            return 'Not saved: only designs were exported. Check "Also save other files" to save it.'
        if 'no fusion design' in text or 'not a fusion document' in text:
            return 'Nothing to do: not a 3D design.'
        if 'unsaved changes' in text or 'close it' in text:
            return 'Save or close it in Fusion, then export again.'
        if 'cannot export this file type' in text or 'offers no export' in text:
            return 'If you need it, open it in Fusion and export or download it by hand.'
        if 'download' in text or 'not saved' in text:
            return 'Export again to retry; if it is skipped again, download it from Fusion by hand.'
        return 'Nothing to do unless you need this file; open it in Fusion to check it.'
    if status == 'unreadable':
        return 'Check you can open this folder in Fusion, then export again.'
    if 'unsaved changes' in text or 'close it' in text or 'close this' in text:
        return 'Save or close this design in Fusion, then export again.'
    if 'disk is full' in text or 'no space' in text:
        return 'Free some disk space, then export again.'
    if 'linked design is missing' in text:
        return ('Open the design in Fusion and fix its links (a linked design was deleted, moved or cannot be '
                'read), then export again. The linked designs themselves are exported in their own right.')
    return 'Export again to retry; if it keeps failing, open the design in Fusion to check it.'


def items(manifest, run_info=None):
    """One dict per manifest entry plus one per unreadable folder, failures first. Older versions
    moved or deleted on purpose (status "moved" or "removed") are not items: older_versions()
    lists them."""
    result = []
    assemblies = {id(item['entry']): item['inserted'] for item in inserted_designs(manifest)}
    for entry in manifest.get('entries', []):
        status = shown_status(entry)
        if status in (REMOVED, MOVED):
            continue
        files = [f.get('path') for f in entry.get('files', []) if f.get('path')]
        roles = [file_role(f) for f in entry.get('files', []) if f.get('path')]
        step = entry_step(entry) if status == 'done' else dict(warnings=[], note=None)
        warnings = step['warnings']
        picture = (entry.get('thumbnail') or {}).get('path') if status == 'done' else None
        links = entry.get('references')
        result.append(dict(status=status, kind='file' if entry.get('kind') == 'file' else 'design',
                           hub=entry.get('hub') or '', project=entry.get('project') or '',
                           folder='/'.join(entry.get('path') or []), name=entry.get('name') or '',
                           version=entry.get('version'), files=files, reason=entry.get('error') or '',
                           next_step=(STEP_RETRIED if status == 'done' and entry.get('step') == STEP_FAILED
                                      else WARNING_STEP if warnings else next_step(status, entry.get('error'))),
                           step_off=status == 'done' and entry.get('step') == STEP_OFF,
                           archive_off=status == 'done' and entry.get('archive') == ARCHIVE_OFF,
                           warnings=warnings, note=step['note'] or '', thumbnail=picture or '',
                           has_links=entry.get('has_references'), links=len(links) if links else 0,
                           links_out_of_date=bool(entry.get('references_out_of_date')),
                           links_unreadable=bool(entry.get('references_unreadable')),
                           fusion_error=entry.get('fusion_error') or '', slow_seconds=entry.get('slow_seconds'),
                           roles=roles, account_changed=bool(entry.get('account_changed')),
                           kept=older_kept_text(entry), inserted=assemblies.get(id(entry)) or [],
                           f3z=bundle_text(entry) if status == 'done' else '',
                           up_axis=up_axis_of(entry) if status == 'done' else None))
    for problem in (run_info or {}).get('discovery_errors') or []:
        where = list(problem.get('path') or [])
        result.append(dict(status='unreadable', kind='folder', hub=where[0] if where else '',
                           project=where[1] if len(where) > 1 else '', folder='/'.join(where[2:]),
                           name='(folder)', version=None, files=[], reason=problem.get('error') or '',
                           next_step=next_step('unreadable', problem.get('error')), warnings=[], note='',
                           thumbnail='', has_links=None, links=0, links_out_of_date=False, links_unreadable=False,
                           fusion_error='', slow_seconds=None, roles=[], account_changed=False, kept='',
                           inserted=[], f3z='', up_axis=None))
    result.sort(key=lambda i: (ORDER.get('warning' if i['warnings'] else i['status'], 9), i['hub'].casefold(),
                               i['project'].casefold(), i['folder'].casefold(), i['name'].casefold()))
    return result


def up_axis_of(entry):
    """An entry's recorded up axis, "y" or "z"; None when unknown (unreadable, or recorded
    before the field existed)."""
    axis = entry.get('up_axis')
    return axis if axis in ('y', 'z') else None


# The report's words for older versions moved or deleted once each design's newest version was
# saved ("Older versions of a design"), by status.
OLDER_WORDS = {
    MOVED: dict(heading='Older versions moved to the %s folder' % OLDER_FOLDER, verb='moved', field='moved',
                files='moved_files', error='move_error',
                note=("Older versions of these designs were moved into the \u201c%s\u201d folder at the top of "
                      "this folder after each design's newest version was saved, under the same folder path. "
                      "Their manifest entries stay, so this list says what went where and when." % OLDER_FOLDER)),
    REMOVED: dict(heading='Deleted older versions', verb='deleted', field='removed', files='removed_files',
                  error='remove_error',
                  note=("Older versions of these designs were deleted from this folder after each design's "
                        "newest version was saved (\u201cOlder versions of a design: Delete\u201d). Their "
                        "manifest entries stay, so this list says what was deleted and when.")),
}


def removed_text(versions, files):
    """'Deleted 2 older versions (5 files).'"""
    return 'Deleted %s (%s).' % (plural(versions, 'older version'), plural(files, 'file'))


def moved_text(versions, files):
    """'Moved 2 older versions (5 files) to Older versions.'"""
    return 'Moved %s (%s) to %s.' % (plural(versions, 'older version'), plural(files, 'file'), OLDER_FOLDER)


def older_kept_text(entry):
    """Why an older version's files were left in place when they would have been moved or
    deleted, or ''."""
    if entry.get('status') != 'done':
        return ''
    reason = entry.get('kept') or ''
    if entry.get('remove_error'):
        reason = 'kept: could not be deleted (%s)' % entry['remove_error']
    if entry.get('move_error'):
        reason = 'kept: could not be moved (%s)' % entry['move_error']
    if not reason:
        return ''
    return 'Older version %s; a newer version of this design is in the folder.' % reason


def older_versions(manifest, status):
    """The older versions moved (status "moved") or deleted (status "removed"), by name."""
    found = [entry for entry in manifest.get('entries', []) if entry.get('status') == status]
    found.sort(key=lambda e: (str(e.get('project') or '').casefold(), '/'.join(e.get('path') or []).casefold(),
                              str(e.get('name') or '').casefold(), e.get('version') or 0))
    return found


def removed_versions(manifest):
    """The older versions deleted from the folder (entries with status "removed"), by name."""
    return older_versions(manifest, REMOVED)


def older_files(entry, status):
    """An older version's moved or deleted files as text: "a.f3d" or "a.f3d \u2192 Older versions/a.f3d"."""
    out = []
    for info in entry.get(OLDER_WORDS[status]['files']) or []:
        if not info.get('path'):
            continue
        out.append(info['path'] + (' \u2192 ' + info['moved_to'] if info.get('moved_to') else ''))
    return out


def older_html(manifest, status):
    """The collapsed section listing the older versions moved or deleted, or nothing."""
    found = older_versions(manifest, status)
    if not found:
        return []
    words = OLDER_WORDS[status]
    esc = html.escape
    count = sum(len(entry.get(words['files']) or []) for entry in found)
    out = ['<details><summary><h2 style="display:inline">%s (%s, %s)</h2></summary>'
           % (esc(words['heading']), plural(len(found), 'version'), plural(count, 'file')),
           '<p class="muted">%s</p><ul>' % esc(words['note'])]
    for entry in found:
        where = ' / '.join(part for part in (entry.get('project') or '', '/'.join(entry.get('path') or [])) if part)
        paths = older_files(entry, status)
        line = '%s v%s <span class="muted">(%s), %s %s</span>' % (
            esc(str(entry.get('name') or '')), esc(str(entry.get('version'))), esc(where), words['verb'],
            esc(str(entry.get(words['field']) or '')[:10]))
        if paths:
            line += '<br><span class="muted">%s</span>' % esc('; '.join(paths))
        if entry.get(words['error']):
            line += '<br><span class="warn">Not %s: %s</span>' % (words['verb'], esc(entry[words['error']]))
        out.append('<li>%s</li>' % line)
    out.append('</ul></details>')
    return out


def shown_status(entry):
    """An entry's status as reported: a skip of a file type Fusion cannot export, recorded by a
    version before "unsupported" existed, is reported as unsupported too."""
    status = entry.get('status')
    if status == 'skipped' and entry.get('kind') == 'file' and str(entry.get('error') or '').startswith(
            LEGACY_NATIVE_REASON):
        return 'unsupported'
    return status


def unsupported_count(manifest):
    """How many files in the folder Fusion's API cannot export."""
    return sum(1 for entry in manifest.get('entries', []) if shown_status(entry) == 'unsupported')


def link(path):
    """A relative URL for a manifest file path (forward slashes), safe for any name."""
    return '/'.join(quote(part) for part in path.split('/'))


STYLE = '''
:root { color-scheme: light dark; --bg: #fff; --text: #1f2328; --muted: #5d6670; --line: #d8dee4;
  --bad: #b42318; --warn: #8a5a00; --good: #1a7f37; --link: #0a5fb4; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16181c; --text: #e3e6ea; --muted: #9aa4b1; --line: #343a42;
    --bad: #ff8178; --warn: #e3b341; --good: #56d364; --link: #79b8ff; } }
body { background: var(--bg); color: var(--text); margin: 24px;
  font: 14px/1.45 -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 24px 0 8px; }
.muted { color: var(--muted); } a { color: var(--link); }
.counts span { display: inline-block; margin: 8px 16px 0 0; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; vertical-align: top;
  padding: 6px 8px; border-bottom: 1px solid var(--line); } th { color: var(--muted); font-weight: 600; }
.failed, .unreadable { color: var(--bad); font-weight: 600; } .skipped { color: var(--warn); }
.unsupported { color: var(--muted); } .done { color: var(--good); } img.thumb { width: 64px; height: 64px; object-fit: contain;
  float: left; margin-right: 8px; } .warn { color: var(--warn); } .stop { border-left: 4px solid var(--bad); padding: 4px 12px; }
'''


def counts_html(counts):
    return ('<div class="counts"><span>Exported now: <b>%d</b></span><span>Already up to date: <b>%d</b></span>'
            '<span>Other files saved: <b>%d</b></span><span>Other files up to date: <b>%d</b></span>'
            '<span>Skipped: <b>%d</b></span><span>Can\'t be exported by an add-in: <b>%d</b></span>'
            '<span>Failed: <b>%d</b></span></div>'
            % (counts.get('exported', 0), counts.get('up_to_date', 0), counts.get('files', 0),
               counts.get('files_up_to_date', 0), counts.get('skipped', 0), counts.get('unsupported', 0),
               counts.get('failed', 0)))


def table_html(rows, heading, column):
    """One table of items (designs, or other files)."""
    esc = html.escape
    out = ['<h2>%s</h2><table><thead><tr><th>Status</th><th>Project</th><th>Folder</th><th>%s</th>'
           '<th>Files</th><th>Reason and next step</th></tr></thead><tbody>' % (esc(heading), esc(column))]
    for row in rows:
        roles = row.get('roles') or [None] * len(row['files'])
        files = ' '.join('<a href="%s">%s</a>' % (esc(link(path)), esc(ROLE_LABEL.get(role) if row['kind'] == 'design'
                                                                        and role in ROLE_LABEL else (
                                                                            path.rsplit('.', 1)[-1].upper()
                                                                            if '.' in path.rsplit('/', 1)[-1] else 'File')))
                         for path, role in zip(row['files'], roles))
        if row.get('step_off'):
            files += (' ' if files else '') + '<span class="muted">%s</span>' % esc(STEP_NOT_REQUESTED)
        if row.get('archive_off'):
            files += (' ' if files else '') + '<span class="muted">%s</span>' % esc(ARCHIVE_NOT_REQUESTED)
        version = ' v%s' % row['version'] if row['version'] is not None and row['kind'] == 'design' else ''
        name = esc(row['name'] + version)
        if row['kind'] == 'file' and row['version'] is not None:
            name += ' <span class="muted">(version %s)</span>' % esc(str(row['version']))
        if row.get('up_axis') == 'y':
            name += ' <span class="muted">%s</span>' % esc(Y_UP)  # Z up is the usual: not shown
        if row['thumbnail']:
            name = '<img class="thumb" src="%s" alt="" loading="lazy">%s' % (esc(link(row['thumbnail'])), name)
        why = esc(row['reason'])
        for warning in row['warnings']:
            why += ('<br>' if why else '') + '<span class="warn">Warning: ' + esc(warning) + '</span>'
        if row.get('note'):
            why += ('<br>' if why else '') + '<span class="muted">Note: ' + esc(row['note']) + '</span>'
        if row['fusion_error']:
            why += ('<br>' if why else '') + '<span class="muted">Fusion said: ' + esc(row['fusion_error']) + '</span>'
        if row['links_unreadable']:
            text = LINKS_UNREADABLE if row['status'] == 'done' else 'Links to other designs could not be read.'
            why += ('<br>' if why else '') + '<span class="warn">Warning: ' + esc(text) + '</span>'
        if row['links_out_of_date']:
            why += ('<br>' if why else '') + '<span class="muted">Note: ' + esc(LINKS_OUT_OF_DATE) + '</span>'
        if row.get('kept'):
            why += ('<br>' if why else '') + '<span class="muted">Note: ' + esc(row['kept']) + '</span>'
        if row.get('account_changed'):
            why += ('<br>' if why else '') + '<span class="warn">Warning: ' + esc(ACCOUNT_CHANGED_NOTE) + '</span>'
        if row['next_step']:
            why += ('<br>' if why else '') + '<span class="muted">' + esc(row['next_step']) + '</span>'
        label = LABEL.get(row['status'], row['status'] or '')
        if row['status'] == 'done' and row['kind'] == 'file':
            label = 'Saved'
        out.append('<tr><td class="%s">%s</td><td>%s<div class="muted">%s</div></td><td>%s</td><td>%s</td>'
                   '<td>%s</td><td>%s</td></tr>'
                   % (esc(row['status'] or ''), esc(label + (', with a warning' if row['warnings'] else '')),
                      esc(row['project']), esc(row['hub']), esc(row['folder']), name, files, why))
    if not rows:
        out.append('<tr><td colspan="6" class="muted">Nothing exported yet.</td></tr>')
    out.append('</tbody></table>')
    return out


def render_html(manifest, run_info, counts):
    rows = items(manifest, run_info)
    esc = html.escape
    out = ['<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>Fusion Bulk Export report</title><style>' + STYLE + '</style></head><body>',
           '<h1>Fusion Bulk Export report</h1>']
    finished = (run_info or {}).get('finished') or ''
    out.append('<div class="muted">Last run finished %s. This page lists everything in this folder\'s '
               'manifest.json.</div>' % esc(finished))
    if counts:
        out.append(counts_html(counts))
    if (run_info or {}).get('stopped_reason'):
        from .errors import NEXT_STEP
        reason = run_info['stopped_reason'].rstrip()
        reason += '' if reason.endswith(('.', '?', '!')) else '.'
        then = (NEXT_STEP.get(run_info.get('stopped_code'))
                if run_info.get('stopped_code') in ('memory', 'account_changed') else None)
        out.append('<p class="stop">The run stopped itself: %s</p>' % esc(reason + (' ' + then if then else '')))
    designs = [row for row in rows if row['kind'] != 'file']
    others = [row for row in rows if row['kind'] == 'file']
    totals = {}
    for row in designs:
        totals[row['status']] = totals.get(row['status'], 0) + 1
    line = ('In this folder: %s exported, %d failed, %d skipped%s'
            % (plural(totals.get('done', 0), 'design'), totals.get('failed', 0), totals.get('skipped', 0),
               ', %s not read' % plural(totals['unreadable'], 'folder') if totals.get('unreadable') else ''))
    if others:
        saved = sum(1 for row in others if row['status'] == 'done')
        unsupported = sum(1 for row in others if row['status'] == 'unsupported')
        line += '; %s saved, %d not saved' % (plural(saved, 'other file'), len(others) - saved - unsupported)
        if unsupported:
            line += '; ' + unsupported_text(unsupported)
    stale = links_out_of_date(manifest)
    if stale:
        line += '; %d with out-of-date links (a note, not a failure)' % stale
    unread = links_unreadable(manifest)
    if unread:
        line += '; %d whose links could not be read (exported all the same)' % unread
    assemblies = inserted_designs(manifest)
    if assemblies:
        line += '; %s with inserted designs' % plural(len(assemblies), 'assembly')
    out.append('<div class="muted">%s.</div>' % esc(line))
    if (run_info or {}).get('step') is False:
        out.append('<p class="muted">Note: %s</p>' % esc(STEP_OFF_NOTE))
    if (run_info or {}).get('archive') is False:
        out.append('<p class="muted">Note: %s</p>' % esc(ARCHIVE_OFF_NOTE))
    hidden = hidden_note(hidden_left_out(manifest))
    if hidden:
        out.append('<p class="muted">Note: %s</p>' % esc(hidden))
    memory = memory_line(run_info)
    if memory:
        out.append('<div class="muted">%s</div>' % esc(memory))
    if low_memory(run_info):
        out.append('<p class="muted">Note: %s</p>' % esc(LOW_MEMORY_NOTE))
    long_paths = long_path_count(manifest)
    if long_paths:
        out.append('<p class="stop">%s</p>' % esc(long_paths_text(long_paths)))
    if manifest.get('older_layout'):
        out.append('<p class="muted">Note: %s</p>' % esc(OLDER_LAYOUT))
    out += slow_html(rows)
    out += assemblies_html(assemblies, manifest, run_info)
    out += table_html(designs, 'Designs', 'Design')
    if others:
        out += table_html(others, 'Other files', 'File')
    out += tool_libraries_html(manifest, run_info)
    out += older_html(manifest, MOVED) + older_html(manifest, REMOVED)
    out.append('</body></html>')
    return '\n'.join(out) + '\n'


NOT_IN_EXPORT = 'not in this export'


def inserted_text(design):
    """An inserted design in words, for the CSV: its .f3d here, or its name and why there is none."""
    if design['path']:
        return design['path']
    return '%s (%s)' % (design['name'] or 'a design', NOT_IN_EXPORT)


def bundle_text(entry):
    """An assembly's .f3z in words, for the CSV: its path, or why none could be built (with the
    path of an earlier one, when there is one)."""
    bundle = bundle_info(entry)
    reason = entry.get('f3z_not_built')
    if reason:
        return '%s(%s: %s)' % (bundle['path'] + ' ' if bundle else '', F3Z_NOT_BUILT, reason)
    return bundle['path'] if bundle else ''


F3Z_NOT_BUILT = '.f3z not built'
F3Z_EARLIER = 'the .f3z beside it is from an earlier build'


def assemblies_note(assemblies, manifest, run_info=None):
    """The assemblies note (checks.inserted_note) with the state of their .f3z files."""
    from .f3z import folder_counts
    return inserted_note(assemblies, folder_counts(manifest, assemblies), bool((run_info or {}).get('f3z_bundles')))


def assemblies_html(assemblies, manifest=None, run_info=None):
    """The "Assemblies with inserted designs" section: each assembly (checks.inserted_designs) by
    name, with a link to its folder and its files (its .f3z too, when one was built beside it, or
    why none could be), and the designs inserted into it, each linked to its own exported .f3d (or
    "not in this export"). Nothing when there are none."""
    if not assemblies:
        return []
    esc = html.escape
    note = assemblies_note(assemblies, manifest if manifest is not None else dict(entries=[]), run_info)
    out = ['<h2>Assemblies with inserted designs</h2><p class="muted">%s</p><ul>' % esc(note)]
    for item in assemblies:
        entry = item['entry']
        files = [info for info in entry.get('files') or [] if info.get('path')]
        archive = next((info['path'] for info in files if file_role(info) == 'archive'), None)
        where = ' / '.join(part for part in (entry.get('project') or '', '/'.join(entry.get('path') or [])) if part)
        if archive and '/' in archive:
            where = '<a href="%s/">%s</a>' % (esc(link(archive.rsplit('/', 1)[0])), esc(where or 'folder'))
        else:
            where = esc(where)
        own = ' '.join('<a href="%s">%s</a>' % (esc(link(info['path'])), esc(ROLE_LABEL[file_role(info)]))
                       for info in files if file_role(info) in ('archive', 'step'))
        bundle = bundle_info(entry)
        if bundle:
            own += (' ' if own else '') + '<a href="%s">%s</a>' % (esc(link(bundle['path'])), ROLE_LABEL['f3z_bundle'])
        if entry.get('f3z_not_built'):
            own += (' ' if own else '') + '<span class="warn">(%s: %s%s)</span>' % (
                F3Z_NOT_BUILT, esc(str(entry['f3z_not_built'])), '; ' + F3Z_EARLIER if bundle else '')
        version = ' v%s' % entry['version'] if entry.get('version') is not None else ''
        inserted = []
        for design in item['inserted']:
            name = esc(design['name'] or 'a design')
            inserted.append('<a href="%s">%s</a>' % (esc(link(design['path'])), name) if design['path']
                            else '%s <span class="muted">(%s)</span>' % (name, NOT_IN_EXPORT))
        out.append('<li><b>%s</b> <span class="muted">(%s)</span>%s<br>Inserted: %s</li>' % (
            esc(str(entry.get('name') or '') + version), where, ' ' + own if own else '', ', '.join(inserted)))
    out.append('</ul>')
    return out


def tool_libraries_html(manifest, run_info=None):
    """The "Tool libraries" section: every CAM tool library saved into this folder, with its
    location, folder, tool count and file, and notes (gone from Fusion, previous copies). Also
    this run's warning when they could not be read at all. Nothing when there are none."""
    found = toollibs.entries(manifest)
    tally = (run_info or {}).get('tool_libraries') or {}
    if not found and not tally.get('warning') and not tally.get('unreadable'):
        return []
    esc = html.escape
    out = ['<h2>Tool libraries</h2>']
    if found:
        out.append('<p class="muted">%s. %s</p>' % (esc(toollibs.count_line(found)), esc(toollibs.IMPORT_HINT)))
    if tally.get('warning'):
        out.append('<p class="warn">%s</p>' % esc('Not exported in the last run. ' + tally['warning']))
    for problem in tally.get('unreadable') or []:
        out.append('<p class="warn">%s</p>' % esc('Could not be read in %s: %s' % (
            problem.get('location') or 'Fusion', problem.get('error') or 'unknown reason')))
    if not found:
        return out
    out.append('<table><thead><tr><th>Status</th><th>Location</th><th>Folder</th><th>Library</th><th>Tools</th>'
               '<th>File</th><th>Notes</th></tr></thead><tbody>')
    for entry in found:
        status = entry.get('status') or ''
        label = 'Saved' if status == 'done' else LABEL.get(status, status)
        path = entry.get('path') or ''
        notes = [esc(entry['error'])] if status == 'failed' and entry.get('error') else []
        if status == 'failed' and path:
            notes.append('<span class="muted">%s</span>' % esc('The file from the last export that worked is kept.'))
        notes += ['<span class="muted">%s</span>' % esc(note) for note in toollibs.entry_notes(entry, run_info)]
        tools = entry.get('tools')
        out.append('<tr><td class="%s">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            esc(status), esc(label), esc(str(entry.get('location') or '')), esc('/'.join(entry.get('folder') or [])),
            esc(str(entry.get('name') or '')), '' if tools is None else esc(str(tools)),
            '<a href="%s">JSON</a>' % esc(link(path)) if path else '', '<br>'.join(notes)))
    out.append('</tbody></table>')
    return out


SLOW_NOTE = ('Fusion took over a minute on each of these (a design locked in another team, or a large '
             'assembly, can block it). Nothing is wrong with the export; this says where the time went.')


def slow_html(rows):
    """The "Slow items" list: items Fusion took over a minute on, slowest first."""
    slow = sorted((row for row in rows if row.get('slow_seconds')), key=lambda row: -row['slow_seconds'])
    if not slow:
        return []
    esc = html.escape
    out = ['<h2>Slow items</h2><p class="muted">%s</p><ul>' % esc(SLOW_NOTE)]
    for row in slow:
        where = ' / '.join(part for part in (row['project'], row['folder']) if part)
        out.append('<li>%s <span class="muted">(%s)</span>: %s</li>'
                   % (esc(row['name']), esc(where), esc(duration_text(row['slow_seconds']))))
    out.append('</ul>')
    return out


def low_memory(run_info):
    """Whether to say the computer was low on memory: lasting pressure Fusion did not cause, in a
    run that did not stop for memory (whose own note says what to do)."""
    run_info = run_info or {}
    return bool(run_info.get('low_memory')) and run_info.get('stopped_code') != 'memory'


def memory_line(run_info):
    """'Fusion memory: 1.4 GB (at most 1.6 GB during the last run, 0.8 GB at its start).', from
    run_info['memory'], or None when it was not measured."""
    from .memory import gigabytes
    facts = (run_info or {}).get('memory') or {}
    last, peak, start = (gigabytes(facts.get(key)) for key in ('last_rss', 'peak_rss', 'start_rss'))
    if last is None:
        return None
    more = []
    if peak is not None and peak != last:
        more.append('at most %s during the last run' % peak)
    if start is not None:
        more.append('%s at its start' % start)
    return 'Fusion memory: %s%s.' % (last, ' (%s)' % ', '.join(more) if more else '')


def duration_text(seconds):
    seconds = int(seconds or 0)
    return '%d s' % seconds if seconds < 120 else '%d min %02d s' % (seconds // 60, seconds % 60)


def slow_count(manifest):
    return sum(1 for entry in manifest.get('entries', []) if entry.get('slow_seconds'))


def render_csv(manifest, run_info):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow(['status', 'hub', 'project', 'folder', 'design', 'version', 'archive', 'step',
                     'reason', 'next_step', 'warnings', 'thumbnail', 'kind', 'file', 'has_links', 'links',
                     'links_out_of_date', 'fusion_error', 'links_unreadable', 'slow_seconds', 'note',
                     'location', 'tools', 'inserted_designs', 'f3z', 'up_axis'])
    for row in items(manifest, run_info):
        design = row['kind'] != 'file'
        by_role = {}
        for path, role in zip(row['files'], row['roles']):
            by_role.setdefault(role, path)
        archive = (ARCHIVE_NOT_REQUESTED if row.get('archive_off') else by_role.get('archive', '')) if design else ''
        step = (STEP_NOT_REQUESTED if row.get('step_off') else by_role.get('step', '')) if design else ''
        writer.writerow([row['status'], row['hub'], row['project'], row['folder'], row['name'],
                         '' if row['version'] is None else row['version'], archive, step, row['reason'],
                         row['next_step'], '; '.join(row['warnings']), row['thumbnail'], row['kind'],
                         '' if design else (row['files'][0] if row['files'] else ''),
                         yes_no(row['has_links']), row['links'] or '', 'yes' if row['links_out_of_date'] else '',
                         row['fusion_error'], 'yes' if row['links_unreadable'] else '',
                         row['slow_seconds'] or '', row.get('note') or row.get('kept') or '', '', '',
                         '; '.join(inserted_text(design) for design in row['inserted']), row['f3z'],
                         row.get('up_axis') or ''])
    for status, reason, label in ((MOVED, 'Older version moved to the %s folder on %%s' % OLDER_FOLDER, 'Moved: '),
                                  (REMOVED, 'Older version deleted from this folder on %s', 'Deleted: ')):
        # One row per moved or deleted older version: its status, and its files in note.
        for entry in older_versions(manifest, status):
            paths = older_files(entry, status)
            writer.writerow([status, entry.get('hub') or '', entry.get('project') or '',
                             '/'.join(entry.get('path') or []), entry.get('name') or '',
                             '' if entry.get('version') is None else entry['version'], '', '',
                             reason % str(entry.get(OLDER_WORDS[status]['field']) or '')[:10],
                             '', '', '', 'design', '', '', '', '', '', '', '',
                             (label + '; '.join(paths)) if paths else '', '', '', '', '', ''])
    for entry in toollibs.entries(manifest):
        # One row per tool library (kind tool_library): its location and tool count in the last
        # two columns, its name in "design", its .json in "file".
        tools = entry.get('tools')
        writer.writerow([entry.get('status') or '', '', '', '/'.join(entry.get('folder') or []), entry.get('name') or '',
                         '', '', '', (entry.get('error') or '') if entry.get('status') == 'failed' else '', '', '', '',
                         toollibs.KIND, entry.get('path') or '', '', '', '', '', '', '',
                         '; '.join(toollibs.entry_notes(entry, run_info)), entry.get('location') or '',
                         '' if tools is None else tools, '', '', ''])
    return stream.getvalue()


def yes_no(value):
    """A CSV cell for a fact that may be unknown: yes, no, or empty."""
    return '' if value is None else ('yes' if value else 'no')


def _write(path, text, encoding):
    temporary = str(path) + '.tmp'
    with open(long_path(temporary), 'w', encoding=encoding, newline='') as stream:
        stream.write(text)
    os.replace(long_path(temporary), long_path(path))


def write(exporter, counts=None):
    """Write index.html and index.csv into the exporter's folder; returns their paths."""
    root = exporter.root
    run_info = exporter.run_info
    page, table = root / 'index.html', root / 'index.csv'
    _write(page, render_html(exporter.manifest, run_info, counts or run_info.get('counts')), 'utf-8')
    _write(table, render_csv(exporter.manifest, run_info), 'utf-8-sig')  # utf-8-sig: Excel reads it
    return page, table


OLDER_LAYOUT = ('Older exports keep their old folder names; new items go in readable folders. Both are in '
                'this folder and listed here.')


def long_paths_text(count):
    """How many items have a path over 260 characters, and what that means."""
    return ('%s %s a path over 260 characters: %s saved, but Explorer and some programs cannot open %s. '
            'A shorter output folder (such as C:\\FusionExport) avoids this.'
            % (plural(count, 'item'), form(count, 'has'), 'it is' if count == 1 else 'they are', form(count, 'it')))


def links_out_of_date(manifest):
    """How many designs in the folder use an older version of a linked design (a note)."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('kind', 'design') == 'design'
               and entry.get('references_out_of_date'))


def links_unreadable(manifest):
    """How many designs were exported although their links could not be read (a warning)."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('kind', 'design') == 'design'
               and entry.get('status') == 'done' and entry.get('references_unreadable'))


def long_path_count(manifest):
    return sum(1 for entry in manifest.get('entries', []) if entry.get('status') == 'done' and entry.get('long_path'))


FAILURES_SHOWN = 3  # failures named in the results dialog; the rest are in the report


def failures(manifest, run_info=None):
    """[{"name", "reason"}] for every failed design and unreadable folder, in report order."""
    found = []
    for row in items(manifest, run_info):
        if row['status'] not in ('failed', 'unreadable'):
            break
        name = row['name'] if row['status'] == 'failed' else ' / '.join(
            part for part in (row['project'], row['folder']) if part)
        found.append(dict(name=name, reason=row['reason'] or 'unknown reason'))
    return found


def warned(manifest):
    """How many exported items carry a warning (a thin STEP, links that could not be read, an
    account that changed). A design with no solid bodies, whose STEP is empty, is not one."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('status') == 'done'
               and (entry_step(entry)['warnings'] or entry.get('references_unreadable')
                    or entry.get('account_changed')))


def hidden_left_out(manifest):
    """How many exported designs have a STEP without their hidden bodies (this Fusion could not
    include them)."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('status') == 'done'
               and entry_step(entry)['hidden_left_out'])


def hidden_note(count):
    """The summary's one line about them, or None when there are none."""
    if not count:
        return None
    return ('%s %s a STEP file without %s hidden bodies: this version of Fusion cannot include them. '
            'The .f3d holds every body.' % (plural(count, 'design'), form(count, 'has'),
                                             form(count, 'its', 'their')))


def older_notes(run_info):
    """The summary's lines about older versions moved or deleted in this run ("Older versions of
    a design"): how many, and any kept or not moved or deleted."""
    run_info = run_info or {}
    moved, removed = run_info.get(MOVED) or {}, run_info.get(REMOVED) or {}
    notes = []
    if moved.get('versions'):
        notes.append(moved_text(moved['versions'], moved.get('files', 0)))
    if removed.get('versions'):
        notes.append(removed_text(removed['versions'], removed.get('files', 0)))
    kept = moved.get('kept', 0) + removed.get('kept', 0)
    if kept:
        notes.append('%s kept as %s (changed since %s exported); the report says which.'
                     % (plural(kept, 'older version'), form(kept, 'it was', 'they were'),
                        form(kept, 'it was', 'they were')))
    for tally, verb in ((moved, 'moved'), (removed, 'deleted')):
        if tally.get('errors'):
            notes.append('%s could not be %s; the next run tries again (see the report).'
                         % (plural(tally['errors'], 'older version'), verb))
    return notes


def summary(counts, run_info, manifest, output, page=None):
    """What the results dialog shows at the end of a run, as data: a one-line headline, the
    counts (zero rows left out, except "Exported now"), up to three failures by name with how
    many more the report lists, the retry hint when something failed, short notes, the output
    folder and, when it was written, the report."""
    run_info = run_info or {}
    if was_canceled(run_info):
        headline = 'Canceled'
    elif run_info.get('stopped_reason'):
        headline = 'Stopped: ' + run_info['stopped_reason']
    else:
        headline = 'Finished'
    rows = [['Exported now', counts.get('exported', 0)]]
    for label, value in (('Already up to date', counts.get('up_to_date', 0)),
                         ('Other files saved', counts.get('files', 0)),
                         ('Other files up to date', counts.get('files_up_to_date', 0)),
                         ('Skipped', counts.get('skipped', 0)),
                         ('Failed', counts.get('failed', 0)),
                         ('Warnings', warned(manifest))):
        if value:
            rows.append([label, value])
    failed = failures(manifest, run_info)
    notes = []
    if was_canceled(run_info):
        notes.append('Run again to carry on where it stopped.')
    if run_info.get('stopped_code'):
        from .errors import NEXT_STEP
        if NEXT_STEP.get(run_info['stopped_code']):
            notes.append(NEXT_STEP[run_info['stopped_code']])
    notes += toollibs.summary_notes(run_info.get('tool_libraries'))
    if run_info.get('low_space'):
        free = run_info.get('free_bytes')
        notes.append('Low disk space: %s free.' % ('%d MB' % (free >> 20) if free is not None else 'little'))
    if low_memory(run_info):
        notes.append(LOW_MEMORY_NOTE)
    long_paths = long_path_count(manifest)
    if long_paths:
        notes.append(long_paths_text(long_paths))
    unsupported = unsupported_count(manifest)
    if unsupported:
        notes.append(unsupported_note(unsupported))
    if run_info.get('step') is False:
        notes.append(STEP_OFF_NOTE)
    if run_info.get('archive') is False:
        notes.append(ARCHIVE_OFF_NOTE)
    notes += older_notes(run_info)
    hidden = hidden_note(hidden_left_out(manifest))
    if hidden:
        notes.append(hidden)
    stale = links_out_of_date(manifest)
    if stale:
        notes.append('%s %s older versions of linked designs (a note, not a failure).'
                     % (plural(stale, 'design'), form(stale, 'uses')))
    assemblies = inserted_designs(manifest)
    if assemblies:
        notes.append(assemblies_note(assemblies, manifest, run_info))
    notes += bundle_notes(run_info.get('f3z'))
    if run_info.get('upgraded_schema'):
        notes.append(OLDER_LAYOUT)
    slow = run_info.get('slow') or 0  # this run's; the report lists every one in the folder
    if slow:
        notes.append('%s took Fusion over a minute%s; the report lists %s under \u201cSlow items\u201d.'
                     % (plural(slow, 'item'), '' if slow == 1 else ' each', form(slow, 'it', 'them')))
    unreadable = len(run_info.get('discovery_errors') or [])
    if unreadable:
        notes.append('%s could not be read.' % plural(unreadable, 'folder'))
    return dict(headline=headline, rows=rows, failures=failed[:FAILURES_SHOWN],
                more=max(len(failed) - FAILURES_SHOWN, 0),
                retry=('Run again to retry the %d failed.' % counts['failed']) if counts.get('failed') else '',
                notes=notes, output=str(output), report=str(page) if page else '')


def bundle_notes(tally):
    """The summary's lines about the previous .f3z files this run replaced, and a pass that
    stopped (the assemblies note says how many were built)."""
    if not isinstance(tally, dict):
        return []
    notes = []
    changed = []
    for key, words in (('moved', 'moved to ' + OLDER_FOLDER), ('kept', 'kept beside the new one'),
                       ('removed', 'replaced')):
        if tally.get(key):
            changed.append('%s %s' % (plural(tally[key], 'previous .f3z file'), words))
    if changed:
        notes.append('Rebuilt .f3z files: %s.' % ', '.join(changed))
    if tally.get('error'):
        notes.append('Building the .f3z files stopped (%s); export again to build the rest.' % tally['error'])
    return notes


def text_of(data):
    """The plain text of summary() data: the fallback dialog's text, and the log's."""
    lines = [data['headline'], '']
    width = max(len(label) for label, _ in data['rows'])
    lines += ['%s  %d' % (label.ljust(width), value) for label, value in data['rows']]
    if data['failures']:
        lines += [''] + ['- %s: %s' % (item['name'], item['reason']) for item in data['failures']]
        if data['more']:
            lines.append('and %d more in the report.' % data['more'])
    if data['retry']:
        lines += ['', data['retry']]
    if data['notes']:
        lines += [''] + data['notes']
    lines += ['', 'Folder: ' + data['output']]
    if data['report']:
        lines.append('Report: ' + data['report'])
    return '\n'.join(lines)


def summary_text(counts, run_info, manifest, output, page=None):
    """The end-of-run summary as plain text (see summary())."""
    return text_of(summary(counts, run_info, manifest, output, page))


def open_path(path, platform=None, run=None, startfile=None):
    """Show a file or folder in the system's file browser or default app."""
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        (startfile or getattr(os, 'startfile'))(str(path))
    elif platform == 'darwin':
        (run or subprocess.Popen)(['open', str(path)])
    else:
        (run or subprocess.Popen)(['xdg-open', str(path)])
