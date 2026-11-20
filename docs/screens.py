"""Draw the README's dialog illustrations from made-up data (no Fusion, no real account).

    python docs/screens.py          # writes docs/dialog.svg and docs/progress.svg;
                                    # with cairosvg installed also docs/dialog.png and docs/progress.png

They follow the real dialogs: the inputs picker.py adds (version line, Save to, Browse…, the
folder note, the other-files checkbox, the Export choice), the tree page (ui/tree.html and
ui/tree.js: hint, filter box, hubs, projects and folders with tri-state boxes, file counts and
"N exported" marks), the progress text of command.describe() and the end-of-run text of
report.summary_text(). tests/test_screens.py checks that the words drawn here are the ones the
add-in uses. Every hub, project, folder and design name is invented.

Fonts: Noto Sans (falls back to the system sans-serif).
"""
from html import escape
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent / 'FusionBulkExport'
FONT = "'Noto Sans', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"

# Backdrop (as the README cards) and a light Fusion dialog.
BACK, BACK2 = '#0f1720', '#16212d'
DIALOG, HEADER, LINE, INK, MUTED = '#f4f5f7', '#e4e7eb', '#c4ccd5', '#1f2328', '#5d6670'
FIELD, TICK, GOOD, ACCENT, BOX = '#ffffff', '#0a6fc2', '#1a7f37', '#f08a24', '#6b7480'

# The words the add-in shows (checked against the code by tests/test_screens.py).
VERSION = 'Fusion Bulk Export 0.3.0'
SAVE_TO = 'Save to'
BROWSE = 'Browse…'
OTHER_FILES = 'Also save other files (PDFs, uploads)'
DRAWINGS = 'Also export drawings as PDF (slow: each drawing can take minutes)'
EXPORT_LABEL = 'Export'
CHOOSE = 'Choose projects or folders'
TREE_HINT = ('Tick what to export. Ticking a folder includes everything inside it. '
             'Keys: arrows move and open, Space ticks, Enter opens.')
FILTER = 'Filter projects by name'
OUTPUT = '/Users/you/Documents/FusionExport'
DONE_BEFORE = 42
CHOSEN = {'p-jigs': None, 'p-arm': [['Brackets']]}  # what the tree below has ticked
OK_BUTTON = 'Export'

# The tree: (depth, kind, state, open, name, files shown, exported mark). state: 0 off, 1 on, 2 some.
TREE = [
    (0, 'hub', 2, True, 'My Hub', None, 0),
    (1, 'project', 2, True, 'Robot Arm', 14, 9),
    (2, 'folder', 1, False, 'Brackets', None, 0),
    (2, 'folder', 0, False, 'Gripper', None, 0),
    (2, 'folder', 0, False, 'Wiring', None, 0),
    (1, 'project', 0, False, 'Desk Lamp', None, 0),
    (1, 'project', 1, False, 'Shop Jigs', None, 33),
    (0, 'hub', 0, True, 'Makerspace', None, 0),
    (1, 'project', 0, False, 'Go-kart', None, 0),
]
FOCUSED = 2  # the row with keyboard focus

# The progress dialog during a one-project export.
PROGRESS_STATE = dict(phase='design', project=1, projects=1, project_name='Robot Arm', path=['Brackets'],
                      design=14, name='Base plate', folder_files=20, folder_item=8,
                      counts=dict(exported=12, up_to_date=0, files=1, skipped=0, failed=1),
                      elapsed=250, seconds_per_design=3.4)
PROGRESS_TEXT = 'Project 1 of 1, design 14: Base plate\nRobot Arm / Brackets\n4 min · 3.4 s/design · 1 failed'

# The end of a run, as report.summary_text() writes it for this made-up manifest.
SUMMARY_COUNTS = dict(exported=17, up_to_date=9, files=3, skipped=1, failed=1)
SUMMARY_MANIFEST = dict(entries=[
    dict(status='failed', name='Gearbox housing', hub='My Hub', project='Robot Arm', path=['Gripper'],
         error='This design is open in Fusion with unsaved changes; save or close it, then run again.'),
    dict(status='skipped', kind='file', name='Motor board', hub='My Hub', project='Robot Arm', path=['Wiring'],
         error='Fusion cannot export this file type through its API (.fbrd)'),
])
OPEN_CHOICES = 'Yes: open the report (index.html).  No: open the folder.  Cancel: close.'


def package():
    """The add-in's package (report.py and settings.py need no Fusion)."""
    if str(PACKAGE) not in sys.path:
        sys.path.insert(0, str(PACKAGE))


def summary_lines():
    """The summary dialog's text, written by the add-in's own code."""
    package()
    from fusion_bulk_export import report
    text = report.summary_text(SUMMARY_COUNTS, {}, SUMMARY_MANIFEST, OUTPUT)
    return (text + '\n\n' + OPEN_CHOICES).split('\n')


def folder_note():
    package()
    from fusion_bulk_export import settings
    return settings.folder_note(dict(state='continues', done=DONE_BEFORE))


def text(x, y, value, size=12, fill=INK, weight=400, anchor='start', italic=False):
    style = ' font-style="italic"' if italic else ''
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}"{style}>{escape(value)}</text>')


def wrap(value, width, size):
    """Lines of value that fit width pixels (a generous estimate of Noto Sans widths)."""
    limit = max(int(width / (size * 0.52)), 8)
    lines, line = [], ''
    for word in value.split(' '):
        if line and len(line) + 1 + len(word) > limit:
            lines.append(line)
            line = word
        else:
            line = (line + ' ' + word) if line else word
    lines.append(line)
    return lines


def button(x, y, w, label, primary=False):
    fill, ink, stroke = (TICK, '#ffffff', TICK) if primary else ('#ffffff', INK, LINE)
    return (f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="4" fill="{fill}" stroke="{stroke}"/>'
            + text(x + w / 2, y + 17.5, label, 12, ink, 600 if primary else 400, 'middle'))


def frame(x, y, w, h, title):
    """A Fusion-style dialog: title bar with the add-in's cube, body."""
    return (f'<rect x="{x + 3}" y="{y + 6}" width="{w}" height="{h}" rx="8" fill="#000" opacity="0.35"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{DIALOG}" stroke="{LINE}"/>'
            f'<path d="M{x},{y + 8} a8,8 0 0 1 8,-8 h{w - 16} a8,8 0 0 1 8,8 v26 h-{w} z" fill="{HEADER}"/>'
            f'<path d="M{x},{y + 34} h{w}" stroke="{LINE}"/>'
            f'<path d="M{x + 16},{y + 11} l7,3.5 l-7,3.5 l-7,-3.5 z" fill="#f6b26b"/>'
            f'<path d="M{x + 9},{y + 14.5} l7,3.5 v7 l-7,-3.5 z" fill="#d9731a"/>'
            f'<path d="M{x + 23},{y + 14.5} l-7,3.5 v7 l7,-3.5 z" fill="#b85f12"/>'
            + text(x + 32, y + 22, title, 12, INK, 700)
            + text(x + w - 16, y + 22, '×', 16, MUTED, 400, 'middle'))


def caret(x, y, opened, fill=MUTED):
    """The tree's open (pointing down) or closed (pointing right) marker, centred on (x, y)."""
    if opened:
        return f'<path d="M{x - 4},{y - 2} h8 l-4,5 z" fill="{fill}"/>'
    return f'<path d="M{x - 2},{y - 4} v8 l5,-4 z" fill="{fill}"/>'


def checkbox(x, y, state):
    out = [f'<rect x="{x}" y="{y}" width="12" height="12" rx="3" fill="{TICK if state == 1 else "none"}" '
           f'stroke="{TICK if state else BOX}" stroke-width="1.5"/>']
    if state == 1:
        out.append(f'<path d="M{x + 3},{y + 6.2} l2.4,2.4 l4.2,-5" fill="none" stroke="#fff" stroke-width="2" '
                   'stroke-linecap="round" stroke-linejoin="round"/>')
    elif state == 2:
        out.append(f'<rect x="{x + 2.5}" y="{y + 5}" width="7" height="2" fill="{TICK}"/>')
    return ''.join(out)


def icon(x, y, kind):
    if kind == 'hub':
        return f'<circle cx="{x + 6}" cy="{y + 6}" r="6" fill="#6b7480"/>'
    if kind == 'project':
        return f'<rect x="{x}" y="{y}" width="14" height="12" rx="2" fill="{ACCENT}"/>'
    return (f'<path d="M{x},{y + 1.8} h5.6 l1.4,1.8 h7 v8.4 h-14 z" fill="#c9a23f"/>')


def tree_rows(x, y, w):
    out, row_h = [], 24
    for index, (depth, kind, state, opened, name, files, exported) in enumerate(TREE):
        top = y + index * row_h
        left = x + 4 + depth * 16
        if index == FOCUSED:
            out.append(f'<rect x="{x + 1}" y="{top + 1}" width="{w - 2}" height="{row_h - 2}" rx="4" fill="none" '
                       f'stroke="{TICK}" stroke-width="2"/>')
        out.append(caret(left + 7, top + 12, opened))
        out.append(checkbox(left + 20, top + 6, state))
        out.append(icon(left + 38, top + 6, kind))
        out.append(text(left + 58, top + 16, name, 12))
        after = left + 58 + len(name) * 7.0 + 8
        if files is not None:
            label = '%d files' % files
            out.append(text(after, top + 16, label, 12, MUTED))
            after += len(label) * 6.6 + 8
        if exported:
            out.append(text(after, top + 16, '%d exported' % exported, 11, GOOD))
    return ''.join(out), len(TREE) * row_h


def dialog_svg():
    width, height = 760, 640
    x, y, w = 150, 28, 460
    label_w = 150
    cx = x + 16 + label_w          # where controls start
    cw = w - 16 - label_w - 16     # control width
    parts = []
    body = y + 34 + 14
    parts.append(text(x + 16, body + 12, VERSION, 12, INK, 600))
    row = body + 28
    parts.append(text(x + 16, row + 15, SAVE_TO, 12, MUTED))
    parts.append(f'<rect x="{cx}" y="{row}" width="{cw}" height="38" rx="3" fill="{FIELD}" stroke="{LINE}"/>')
    parts.append(text(cx + 8, row + 16, OUTPUT, 12))
    row += 46
    parts.append(button(cx, row, 86, BROWSE))
    row += 34
    parts.append(text(cx, row + 12, folder_note(), 12, MUTED))
    row += 26
    for label, ticked in ((OTHER_FILES, 1), (DRAWINGS, 0)):
        lines = wrap(label, label_w - 6, 12)
        for index, line in enumerate(lines):
            parts.append(text(x + 16, row + 12 + index * 16, line, 12, MUTED))
        parts.append(checkbox(cx, row + 2, ticked))
        row += len(lines) * 16 + 12
    parts.append(text(x + 16, row + 17, EXPORT_LABEL, 12, MUTED))
    parts.append(f'<rect x="{cx}" y="{row}" width="{cw}" height="26" rx="3" fill="{FIELD}" stroke="{LINE}"/>')
    parts.append(text(cx + 8, row + 17, CHOOSE, 12))
    parts.append(caret(cx + cw - 14, row + 13, True))
    row += 36
    # The tree page (an HTML input across the whole dialog).
    tx, tw = x + 12, w - 24
    hint = wrap(TREE_HINT, tw - 8, 12)
    for index, line in enumerate(hint):
        parts.append(text(tx + 4, row + 12 + index * 16, line, 12, MUTED))
    row += len(hint) * 16 + 6
    parts.append(f'<rect x="{tx + 4}" y="{row}" width="{tw - 8}" height="24" rx="4" fill="{FIELD}" stroke="{LINE}"/>')
    parts.append(text(tx + 12, row + 16, FILTER, 12, '#8d97a4'))
    row += 32
    rows, rows_h = tree_rows(tx, row, tw)
    parts.append(rows)
    row += rows_h + 8
    whole = sum(1 for value in CHOSEN.values() if value is None)
    folders = sum(len(value) for value in CHOSEN.values() if value is not None)
    parts.append(text(x + 16, row + 12, '%d whole project(s) and %d folder(s) chosen.' % (whole, folders), 12, MUTED))
    row += 28
    parts.append(f'<path d="M{x},{row} h{w}" stroke="{LINE}"/>')
    parts.append(button(x + w - 16 - 80 - 8 - 80, row + 12, 80, OK_BUTTON, primary=True))
    parts.append(button(x + w - 16 - 80, row + 12, 80, 'Cancel'))
    h = row + 50 - y
    height = y + h + 34
    return svg(width, height, frame(x, y, w, h, 'Fusion Bulk Export') + ''.join(parts))


def progress_svg():
    width = 1140
    parts = []
    # Progress dialog.
    x, y, w = 40, 40, 420
    lines = PROGRESS_TEXT.split('\n')
    body = y + 34 + 18
    inner = []
    for index, line in enumerate(lines):
        inner.append(text(x + 18, body + 12 + index * 18, line, 12, INK if index == 0 else MUTED))
    bar_y = body + len(lines) * 18 + 14
    done = (PROGRESS_STATE['folder_item'] - 1) / float(PROGRESS_STATE['folder_files'])
    inner.append(f'<rect x="{x + 18}" y="{bar_y}" width="{w - 36}" height="10" rx="5" fill="{HEADER}" stroke="{LINE}"/>')
    inner.append(f'<rect x="{x + 18}" y="{bar_y}" width="{(w - 36) * done:.0f}" height="10" rx="5" fill="{TICK}"/>')
    inner.append(button(x + w - 18 - 80, bar_y + 26, 80, 'Cancel'))
    h = bar_y + 26 + 26 + 18 - y
    parts.append(frame(x, y, w, h, 'Fusion Bulk Export') + ''.join(inner))
    # End-of-run summary.
    sx, sy, sw = 500, 40, 600
    inner = []
    tx = sx + 64
    line_y = sy + 34 + 26
    inner.append(f'<circle cx="{sx + 34}" cy="{line_y + 4}" r="14" fill="{TICK}"/>')
    inner.append(text(sx + 34, line_y + 10, 'i', 17, '#ffffff', 700, 'middle'))
    for line in summary_lines():
        if not line:
            line_y += 8
            continue
        for part in wrap(line, sw - 64 - 20, 12):
            inner.append(text(tx, line_y + 8, part, 12, INK, 600 if part.startswith('Export ') else 400))
            line_y += 17
    line_y += 12
    for index, label in enumerate(('Yes', 'No', 'Cancel')):
        inner.append(button(sx + sw - 18 - (3 - index) * 76 + 4, line_y, 72, label, primary=index == 0))
    sh = line_y + 26 + 16 - sy
    parts.append(frame(sx, sy, sw, sh, 'Fusion Bulk Export') + ''.join(inner))
    height = max(y + h, sy + sh) + 40
    return svg(width, height, ''.join(parts))


def svg(width, height, content):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BACK}"/>'
            f'<stop offset="1" stop-color="{BACK2}"/></linearGradient></defs>\n'
            f'<rect width="{width}" height="{height}" rx="12" fill="url(#g)"/>\n{content}\n</svg>\n')


SCREENS = {'dialog': dialog_svg, 'progress': progress_svg}


def main():
    try:
        import cairosvg
    except ImportError:
        cairosvg = None
    for name, draw in SCREENS.items():
        drawing = draw()
        (HERE / (name + '.svg')).write_text(drawing, encoding='utf-8')
        if cairosvg:
            # Twice the size, so the text stays sharp on high-density screens.
            cairosvg.svg2png(bytestring=drawing.encode('utf-8'), write_to=str(HERE / (name + '.png')), scale=2)
        print(name, 'png' if cairosvg else 'svg only')


if __name__ == '__main__':
    main()
