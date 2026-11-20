"""The results dialog shown when an export from the button ends.

A second Fusion command ("Fusion Bulk Export results") whose dialog holds one HTML page
(ui/summary.html and summary.js) across its whole width: the headline, the counts, the first
failures, notes and the output folder, with the buttons Open report and Open folder on the page
and the dialog's own button renamed Close. The page asks for its data over the same channel the
tree page uses (adsk.fusionSendData, answered through incomingFromHTML), so no name ever goes
into a URL.

A command cannot be started reliably from inside another command's execute handler, so the
add-in hands the data over and fires a custom event; the event's handler starts this command
once the export command has finished (FusionBulkExport.py).

Whenever the dialog cannot be shown (an older Fusion, a missing member, any error) the same
summary is shown in a plain message box with an OK button only (fallback()): the summary is
never lost.
"""
import json
from pathlib import Path

from . import errors, report

# The switch: False skips the results dialog entirely (no command is registered, nothing is
# fired) and the summary goes straight to the plain message box (fallback()). Off for now: on real
# Fusion the dialog never appeared, and which mechanism Fusion accepts is still being found out.
DIALOG_ENABLED = False

PAGE = Path(__file__).resolve().parent.parent / 'ui' / 'summary.html'
TITLE = 'Fusion Bulk Export results'
CLOSE = 'Close'
# Dialog size in Fusion's units; the page reads well from about 420 to 700 wide.
SIZE = (480, 420)
MINIMUM = (420, 300)


def page_data(data):
    """What the page is sent: the summary without the report's path (only whether there is one)."""
    shown = dict((key, value) for key, value in data.items() if key not in ('report', 'text'))
    shown['has_report'] = bool(data.get('report'))
    return shown


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
    """Builds the results dialog in command and answers its page. Raises when the dialog cannot
    be built (no HTML inputs in this Fusion, for one): the caller then uses fallback()."""
    def __init__(self, command, data, opener=None, trace=lambda message: None):
        self.command, self.data, self.trace = command, data, trace
        self.opener = opener
        self.shown = False  # set when the page says it shows the results (see closed())
        add_browser = getattr(command.commandInputs, 'addBrowserCommandInput', None)
        if add_browser is None:
            raise RuntimeError('this Fusion has no HTML inputs in command dialogs')
        self.page = add_browser('fbx_results', '', PAGE.as_uri(), 240, 0)
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
        """Answer the page: "summary" (its data), "shown" (it rendered them), "open" ({"what":
        "report" | "folder"}) and "close" (Enter or Escape on the page)."""
        request = json.loads(data) if data else {}
        if action == 'summary':
            return json.dumps(page_data(self.data))
        if action == 'shown':
            self.shown = True
            return json.dumps(dict(ok=True))
        if action == 'open':
            what = request.get('what')
            target = self.data.get('report') if what == 'report' else self.data.get('output')
            if not target:
                return json.dumps(dict(error='There is no report: it could not be written.'))
            try:
                (self.opener or report.open_path)(target)
            except Exception as error:
                return json.dumps(dict(error='Could not open it (%s). Open %s from your file browser.'
                                       % (error, target)))
            return json.dumps(dict(ok=True))
        if action == 'close':
            try:
                self.command.doExecute(True)  # ends the command, closing its dialog
            except Exception as error:
                self.trace('doExecute: %s' % error)
                return json.dumps(dict(error='Use the Close button.'))
            return json.dumps(dict(ok=True))
        return ''


def closed(ui, view):
    """The dialog closed. If its page never showed the results (Fusion did not connect it, or it
    failed), they are shown in a message box now."""
    if view is not None and not view.shown:
        fallback(ui, view.data)


def fallback(ui, data):
    """The summary in a plain message box, OK only, with the folder and report paths as text."""
    text = data.get('text') or report.text_of(data)
    try:
        errors.message_box(ui, text, warning=False)
    except Exception:
        ui.messageBox(text, errors.TITLE)
