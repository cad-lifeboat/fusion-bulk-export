"""The results dialog shown when an export from the button ends.

A second Fusion command ("Fusion Bulk Export results") whose dialog holds one HTML page across
its whole width: the headline, the counts, the first failures, notes and the output folder, with
the buttons Open report and Open folder on the page and the dialog's own button renamed Close.

The page is written for each run (write_page()), with the summary already in it as plain HTML
(every name escaped), into the user's app-data folder: never the export folder, never beside the
add-in, and no name goes into its URL. So the page shows the counts without any message between
it and the add-in: no "ready" handshake that Fusion can drop. The page's script is used for the
two buttons only (adsk.fusionSendData, answered through incomingFromHTML); when a click gets no
answer within 2 s, the page shows the path as text with a Copy button. The page is deleted when
the dialog closes, and any left over at the next start (remove_pages()).

A command cannot be started reliably from inside another command's execute handler, so the
add-in starts this one once the export command has finished (FusionBulkExport.py). Only when the
dialog really cannot be made (the page cannot be written, Fusion will not start the command, or
building the dialog raises) is the same summary shown in a plain message box with an OK button
only (fallback()), and the reason is appended to results-fallback.log beside the add-in.
"""
from html import escape
import json
import os
from pathlib import Path
import time

from . import errors, report, settings

# The switch: False skips the results dialog entirely (no command is registered, nothing is
# fired) and the summary goes straight to the plain message box (fallback()).
DIALOG_ENABLED = True

UI = Path(__file__).resolve().parent.parent / 'ui'
TEMPLATE = UI / 'summary.html'
SCRIPT = UI / 'summary.js'
SUMMARY_MARK = '<!--SUMMARY-->'
SCRIPT_TAG = '<script src="summary.js"></script>'
PAGE_PREFIX = 'results-page-'
FALLBACK_LOG = 'results-fallback.log'
TITLE = 'Fusion Bulk Export results'
CLOSE = 'Close'
SAVED_IN = 'Saved in'
BUTTONS = (('report', 'Open report'), ('folder', 'Open folder'))
# Dialog size in Fusion's units; the page reads well from about 420 to 700 wide.
SIZE = (480, 420)
MINIMUM = (420, 300)


def tone(headline):
    if headline.startswith('Stopped'):
        return 'stopped'
    if headline.startswith('Cancelled'):
        return 'cancelled'
    return 'finished'


def row_class(label):
    """A CSS class for a count row from its label: "Already up to date" -> "already-up-to-date"."""
    words = ''.join(c if c.isalpha() and c.isascii() else ' ' for c in str(label).lower()).split()
    return '-'.join(words)


def number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def render(data):
    """The summary as HTML, every name and path escaped: what the page shows with no script."""
    e = lambda value: escape('' if value is None else str(value), quote=True)
    headline = str(data.get('headline') or 'Finished')
    parts = ['<h1 class="%s">%s</h1>' % (tone(headline), e(headline)), '<div class="counts">']
    for index, row in enumerate(data.get('rows') or []):
        label, value = row[0], number(row[1])
        if index and not value:
            continue  # the first row ("Exported now") always shows; other zero rows do not
        parts.append('<div class="count %s" title="%s"><span class="label">%s</span>'
                     '<span class="value">%d</span></div>' % (row_class(label), e(label), e(label), value))
    parts.append('</div>')
    failures = (data.get('failures') or [])[:report.FAILURES_SHOWN]
    if failures:
        parts.append('<ul class="failures">')
        for failure in failures:
            parts.append('<li class="failure"><span class="name" title="%s">%s:</span>'
                         '<span class="reason">%s</span></li>'
                         % (e(failure.get('name')), e(failure.get('name')), e(failure.get('reason'))))
        parts.append('</ul>')
        if number(data.get('more')):
            parts.append('<div class="more">and %d more in the report</div>' % number(data.get('more')))
    if data.get('retry'):
        parts.append('<div class="retry">%s</div>' % e(data['retry']))
    if data.get('notes'):
        parts.append('<ul class="notes">%s</ul>' % ''.join('<li>%s</li>' % e(note) for note in data['notes']))
    parts.append('<div class="folder-label">%s</div>' % SAVED_IN)
    parts.append('<div class="folder">%s</div>' % e(data.get('output')))
    parts.append('<div class="actions">')
    for what, label in BUTTONS:
        path = data.get('report') if what == 'report' else data.get('output')
        if path:
            parts.append('<button type="button" data-open="%s" data-path="%s">%s</button>' % (what, e(path), label))
    parts.append('</div>')
    parts.append('<div class="status" id="status" role="status"></div>')
    # Shown by the script when a button gets no answer: the path, selectable, with Copy.
    parts.append('<div class="manual" id="manual" hidden><div id="manual-label"></div>'
                 '<div class="manual-row"><input id="manual-path" class="path" readonly value="">'
                 '<button type="button" id="copy">Copy</button></div></div>')
    return '\n'.join(parts)


def page_html(data):
    """The whole page: ui/summary.html with the summary in it and ui/summary.js inlined, so
    the one file needs nothing beside it."""
    template = TEMPLATE.read_text(encoding='utf-8')
    script = SCRIPT.read_text(encoding='utf-8')
    if SUMMARY_MARK not in template or SCRIPT_TAG not in template or '</script' in script.lower():
        raise ValueError('the results page template is damaged')
    return template.replace(SUMMARY_MARK, render(data)).replace(SCRIPT_TAG, '<script>\n' + script + '</script>')


def pages_folder():
    """The user's app-data folder of the add-in (where settings.json is)."""
    return settings.settings_path().parent


def write_page(data, folder=None):
    """Write this run's page; returns its path. Raises OSError when it cannot be written."""
    folder = Path(folder) if folder is not None else pages_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('%s%d-%d.html' % (PAGE_PREFIX, int(time.time() * 1000), os.getpid()))
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(page_html(data), encoding='utf-8')
    os.replace(str(temporary), str(path))
    return path


def remove_pages(folder=None, keep=None):
    """Delete the pages of earlier runs (all but keep). Failures are ignored."""
    try:
        folder = Path(folder) if folder is not None else pages_folder()
        for path in folder.glob(PAGE_PREFIX + '*'):
            if keep is None or path != Path(keep):
                try:
                    path.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def close_button(command, trace=lambda message: None):
    """One button, Close: the OK button hidden and Cancel (which closes, as Escape does) renamed.
    Where this Fusion cannot hide OK, OK is named Close instead. Returns which button it is."""
    try:
        command.isOKButtonVisible = False
    except Exception as error:
        trace('isOKButtonVisible: %s' % error)
        try:
            command.okButtonText = CLOSE
        except Exception as error:
            trace('okButtonText: %s' % error)
        return 'ok'
    try:
        command.cancelButtonText = CLOSE
    except Exception as error:
        trace('cancelButtonText: %s' % error)
    return 'cancel'


class Results:
    """Builds the results dialog in command around the page at page (a written file) and
    answers the page's buttons. Raises when the dialog cannot be built (no HTML inputs in this
    Fusion, for one): the caller then uses fallback()."""
    def __init__(self, command, data, page, opener=None, trace=lambda message: None):
        self.command, self.data, self.trace = command, data, trace
        self.opener = opener
        add_browser = getattr(command.commandInputs, 'addBrowserCommandInput', None)
        if add_browser is None:
            raise RuntimeError('this Fusion has no HTML inputs in command dialogs')
        self.page = add_browser('fbx_results', '', Path(page).resolve().as_uri(), 240, 0)
        trace('results: browser input added')
        try:
            self.page.isFullWidth = True
        except Exception as error:
            trace('isFullWidth: %s' % error)
        for name, size in (('setDialogInitialSize', SIZE), ('setDialogMinimumSize', MINIMUM)):
            try:
                getattr(command, name)(*size)
            except Exception as error:
                trace('%s: %s' % (name, error))
        self.button = close_button(command, trace)
        try:
            command.isRepeatable = False
        except Exception as error:
            trace('isRepeatable: %s' % error)

    def incoming(self, action, data):
        """Answer the page: "open" ({"what": "report" | "folder"}), "close" (Enter or Escape
        on the page) and "ready" (sent once the page has loaded: only logged)."""
        try:
            request = json.loads(data) if data else {}
        except ValueError:
            request = {}
        if not isinstance(request, dict):
            request = {}
        if action == 'open':
            what = request.get('what')
            target = self.data.get('report') if what == 'report' else self.data.get('output')
            if not target:
                return json.dumps(dict(error='There is no report: it could not be written.'))
            try:
                (self.opener or report.open_path)(target)
            except Exception as error:
                return json.dumps(dict(error='Could not open it (%s).' % errors.message_of(error), path=target))
            return json.dumps(dict(ok=True))
        if action == 'close':
            try:
                self.command.doExecute(True)  # ends the command, closing its dialog
            except Exception as error:
                self.trace('doExecute: %s' % error)
                return json.dumps(dict(error='Use the Close button.'))
            return json.dumps(dict(ok=True))
        return json.dumps(dict(ok=True))


def log_fallback(folder, reason):
    """Append why the message box was used to results-fallback.log in folder (no names)."""
    if not folder:
        return None
    path = os.path.join(str(folder), FALLBACK_LOG)
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), ' '.join(str(reason).split())))
        return path
    except OSError:
        return None


FALLBACK_NOTE = 'Nothing is lost: everything above is in the folder, and the report lists it.'


def fallback(ui, data):
    """The summary in a plain message box, OK only, with the folder and report paths as text, and
    a line saying that nothing is lost (the box only stands in for the results dialog)."""
    text = (data.get('text') or report.text_of(data)) + '\n\n' + FALLBACK_NOTE
    try:
        errors.message_box(ui, text, warning=False)
    except Exception:
        ui.messageBox(text, errors.TITLE)
