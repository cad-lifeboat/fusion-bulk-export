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

from .core import LINKS_UNREADABLE
from .names import long_path

ORDER = {'failed': 0, 'unreadable': 1, 'skipped': 2, 'warning': 3, 'done': 4}
LABEL = {'failed': 'Failed', 'unreadable': 'Not read', 'skipped': 'Skipped', 'done': 'Exported'}
LINKS_OUT_OF_DATE = ('Some of its links are out of date: a linked design has a newer version than this design '
                     'uses. The export holds the versions it uses; open it in Fusion and update its links '
                     'if you want the newer ones.')
WARNING_STEP = ('The .f3d archive is the complete copy; open the design in Fusion if you need the STEP '
                'to hold every body.')


def next_step(status, error):
    """What the user can do about an item, in a few words."""
    text = (error or '').lower()
    if status == 'done':
        return ''
    if status == 'skipped':
        if 'drawing pdfs are off' in text:
            return ('Tick "Also export drawings as PDF" (command line: --drawings) and export again to include '
                    'it; each drawing can take minutes.')
        if 'not a fusion design' in text:
            return 'Not saved: only designs were exported. Tick "Also save other files" to save it.'
        if 'no fusion design' in text or 'not a fusion document' in text:
            return 'Nothing to do: not a 3D design.'
        if 'unsaved changes' in text or 'close it' in text:
            return 'Save or close it in Fusion, then export again.'
        if 'drawing pdf' in text:
            return 'Export again to retry; or open the drawing in Fusion and export its PDF by hand.'
        if 'cannot export this file type' in text:
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
    """One dict per manifest entry plus one per unreadable folder, failures first."""
    result = []
    for entry in manifest.get('entries', []):
        status = entry.get('status')
        files = [f.get('path') for f in entry.get('files', []) if f.get('path')]
        warnings = [str(w) for w in entry.get('warnings') or []] if status == 'done' else []
        picture = (entry.get('thumbnail') or {}).get('path') if status == 'done' else None
        links = entry.get('references')
        result.append(dict(status=status, kind='file' if entry.get('kind') == 'file' else 'design',
                           hub=entry.get('hub') or '', project=entry.get('project') or '',
                           folder='/'.join(entry.get('path') or []), name=entry.get('name') or '',
                           version=entry.get('version'), files=files, reason=entry.get('error') or '',
                           next_step=WARNING_STEP if warnings else next_step(status, entry.get('error')),
                           warnings=warnings, thumbnail=picture or '',
                           has_links=entry.get('has_references'), links=len(links) if links else 0,
                           links_out_of_date=bool(entry.get('references_out_of_date')),
                           links_unreadable=bool(entry.get('references_unreadable')),
                           fusion_error=entry.get('fusion_error') or ''))
    for problem in (run_info or {}).get('discovery_errors') or []:
        where = list(problem.get('path') or [])
        result.append(dict(status='unreadable', kind='folder', hub=where[0] if where else '',
                           project=where[1] if len(where) > 1 else '', folder='/'.join(where[2:]),
                           name='(folder)', version=None, files=[], reason=problem.get('error') or '',
                           next_step=next_step('unreadable', problem.get('error')), warnings=[],
                           thumbnail='', has_links=None, links=0, links_out_of_date=False, links_unreadable=False,
                           fusion_error=''))
    result.sort(key=lambda i: (ORDER.get('warning' if i['warnings'] else i['status'], 9), i['hub'].casefold(),
                               i['project'].casefold(), i['folder'].casefold(), i['name'].casefold()))
    return result


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
.done { color: var(--good); } img.thumb { width: 64px; height: 64px; object-fit: contain;
  float: left; margin-right: 8px; } .warn { color: var(--warn); } .stop { border-left: 4px solid var(--bad); padding: 4px 12px; }
'''


def counts_html(counts):
    return ('<div class="counts"><span>Exported now: <b>%d</b></span><span>Already up to date: <b>%d</b></span>'
            '<span>Other files saved: <b>%d</b></span><span>Other files up to date: <b>%d</b></span>'
            '<span>Skipped: <b>%d</b></span><span>Failed: <b>%d</b>'
            '</span></div>' % (counts.get('exported', 0), counts.get('up_to_date', 0), counts.get('files', 0),
                               counts.get('files_up_to_date', 0), counts.get('skipped', 0), counts.get('failed', 0)))


def table_html(rows, heading, column):
    """One table of items (designs, or other files)."""
    esc = html.escape
    out = ['<h2>%s</h2><table><thead><tr><th>Status</th><th>Project</th><th>Folder</th><th>%s</th>'
           '<th>Files</th><th>Reason and next step</th></tr></thead><tbody>' % (esc(heading), esc(column))]
    for row in rows:
        files = ' '.join('<a href="%s">%s</a>' % (esc(link(path)), esc(path.rsplit('.', 1)[-1].upper()
                                                                        if '.' in path.rsplit('/', 1)[-1] else 'File'))
                         for path in row['files'])
        version = ' v%s' % row['version'] if row['version'] is not None and row['kind'] == 'design' else ''
        name = esc(row['name'] + version)
        if row['kind'] == 'file' and row['version'] is not None:
            name += ' <span class="muted">(version %s)</span>' % esc(str(row['version']))
        if row['thumbnail']:
            name = '<img class="thumb" src="%s" alt="" loading="lazy">%s' % (esc(link(row['thumbnail'])), name)
        why = esc(row['reason'])
        for warning in row['warnings']:
            why += ('<br>' if why else '') + '<span class="warn">Warning: ' + esc(warning) + '</span>'
        if row['fusion_error']:
            why += ('<br>' if why else '') + '<span class="muted">Fusion said: ' + esc(row['fusion_error']) + '</span>'
        if row['links_unreadable']:
            text = LINKS_UNREADABLE if row['status'] == 'done' else 'Links to other designs could not be read.'
            why += ('<br>' if why else '') + '<span class="warn">Warning: ' + esc(text) + '</span>'
        if row['links_out_of_date']:
            why += ('<br>' if why else '') + '<span class="muted">Note: ' + esc(LINKS_OUT_OF_DATE) + '</span>'
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
        out.append('<p class="stop">The run stopped itself: %s</p>' % esc(run_info['stopped_reason']))
    designs = [row for row in rows if row['kind'] != 'file']
    others = [row for row in rows if row['kind'] == 'file']
    totals = {}
    for row in designs:
        totals[row['status']] = totals.get(row['status'], 0) + 1
    line = ('In this folder: %d designs exported, %d failed, %d skipped%s'
            % (totals.get('done', 0), totals.get('failed', 0), totals.get('skipped', 0),
               ', %d folders not read' % totals['unreadable'] if totals.get('unreadable') else ''))
    if others:
        saved = sum(1 for row in others if row['status'] == 'done')
        line += '; %d other files saved, %d not saved' % (saved, len(others) - saved)
    stale = links_out_of_date(manifest)
    if stale:
        line += '; %d with out-of-date links (a note, not a failure)' % stale
    unread = links_unreadable(manifest)
    if unread:
        line += '; %d whose links could not be read (exported all the same)' % unread
    out.append('<div class="muted">%s.</div>' % esc(line))
    long_paths = long_path_count(manifest)
    if long_paths:
        out.append('<p class="stop">%s</p>' % esc(LONG_PATHS % long_paths))
    out += table_html(designs, 'Designs', 'Design')
    if others:
        out += table_html(others, 'Other files', 'File')
    out.append('</body></html>')
    return '\n'.join(out) + '\n'


def render_csv(manifest, run_info):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow(['status', 'hub', 'project', 'folder', 'design', 'version', 'archive', 'step',
                     'reason', 'next_step', 'warnings', 'thumbnail', 'kind', 'file', 'has_links', 'links',
                     'links_out_of_date', 'fusion_error', 'links_unreadable'])
    for row in items(manifest, run_info):
        design = row['kind'] != 'file'
        archive = next((p for p in row['files'] if p.lower().endswith('.f3d')), '') if design else ''
        step = next((p for p in row['files'] if p.lower().endswith(('.step', '.stp'))), '') if design else ''
        writer.writerow([row['status'], row['hub'], row['project'], row['folder'], row['name'],
                         '' if row['version'] is None else row['version'], archive, step, row['reason'],
                         row['next_step'], '; '.join(row['warnings']), row['thumbnail'], row['kind'],
                         '' if design else (row['files'][0] if row['files'] else ''),
                         yes_no(row['has_links']), row['links'] or '', 'yes' if row['links_out_of_date'] else '',
                         row['fusion_error'], 'yes' if row['links_unreadable'] else ''])
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


def drawings_off(manifest):
    """How many drawings were left out because drawing PDFs were not asked for."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('kind') == 'file'
               and entry.get('status') == 'skipped' and str(entry.get('error') or '').startswith('Drawing PDFs are off'))


LONG_PATHS = ('%d item(s) have a path over 260 characters: they are saved, but Explorer and some programs '
              'cannot open them. A shorter output folder (such as C:\\FusionExport) avoids this.')


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
    """How many exported items carry a warning (a thin STEP, links that could not be read)."""
    return sum(1 for entry in manifest.get('entries', []) if entry.get('status') == 'done'
               and (entry.get('warnings') or entry.get('references_unreadable')))


def summary(counts, run_info, manifest, output, page=None):
    """What the results dialog shows at the end of a run, as data: a one-line headline, the
    counts (zero rows left out, except "Exported now"), up to three failures by name with how
    many more the report lists, the retry hint when something failed, short notes, the output
    folder and, when it was written, the report."""
    run_info = run_info or {}
    if run_info.get('cancelled'):
        headline = 'Cancelled'
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
    if run_info.get('cancelled'):
        notes.append('Run again to carry on where it stopped.')
    if run_info.get('stopped_code'):
        from .errors import NEXT_STEP
        if NEXT_STEP.get(run_info['stopped_code']):
            notes.append(NEXT_STEP[run_info['stopped_code']])
    if run_info.get('low_space'):
        free = run_info.get('free_bytes')
        notes.append('Low disk space: %s free.' % ('%d MB' % (free >> 20) if free is not None else 'little'))
    long_paths = long_path_count(manifest)
    if long_paths:
        notes.append(LONG_PATHS % long_paths)
    left_out = drawings_off(manifest)
    if left_out:
        notes.append('%d drawing(s) left out: drawing PDFs are off.' % left_out)
    stale = links_out_of_date(manifest)
    if stale:
        notes.append('%d design(s) use older versions of linked designs (a note, not a failure).' % stale)
    unreadable = len(run_info.get('discovery_errors') or [])
    if unreadable:
        notes.append('%d folder(s) could not be read.' % unreadable)
    return dict(headline=headline, rows=rows, failures=failed[:FAILURES_SHOWN],
                more=max(len(failed) - FAILURES_SHOWN, 0),
                retry=('Run again to retry the %d failed.' % counts['failed']) if counts.get('failed') else '',
                notes=notes, output=str(output), report=str(page) if page else '')


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
