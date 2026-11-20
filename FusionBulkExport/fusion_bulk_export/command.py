"""The button's export: folder dialog, progress dialog, summary. Lives in the package (not the
entry file) so a reinstall takes effect at the next dialog without restarting the add-in."""
import os

import adsk
import adsk.core

from . import errors, report
from .core import Exporter
from .fusion import FusionAdapter


def open_exporter(output, adapter):
    """The Exporter for output, or a UserError saying why that folder cannot be used."""
    try:
        return Exporter(output, adapter)
    except Exception as error:
        known = errors.output_folder(error, output)
        if known is None:
            raise
        raise known from error


def has_hubs(app):
    try:
        return app.data.dataHubs.count > 0
    except Exception:
        return False


def describe(state):
    """Progress text: the bar counts projects; the text says where it is and what it is doing."""
    where = ' / '.join([state['project_name'] or ''] + state['path'])
    head = 'Project %d of %d' % (state['project'], state['projects'])
    if state['phase'] == 'listing':
        text = '%s, listing: %s\n%d design(s) so far' % (head, where, state['design'])
    else:
        text = '%s, design %d: %s\n%s' % (head, state['design'], state['name'], where)
    return text.replace('%', '%%')  # % starts a placeholder in Fusion's progress text


OPEN_CHOICES = ('\n\nYes: open the report (index.html).  No: open the folder.  Cancel: close.')


def ask_trouble(ui, reason, last_error):
    """The run cannot go on (disk full, folder gone, designs failing in a row): Retry or Stop."""
    core = adsk.core
    text = ('The export has paused: %s\n\nLast error: %s\n\nRetry carries on. Cancel stops the export: '
            'everything exported so far is kept, and running it again carries on from there.'
            % (reason, last_error or 'none'))
    answer = ui.messageBox(text, 'Fusion Bulk Export', core.MessageBoxButtonTypes.RetryCancelButtonType,
                           core.MessageBoxIconTypes.WarningIconType)
    return 'retry' if answer == core.DialogResults.DialogRetry else 'stop'


def show_summary(ui, text, output, page, opener):
    """The end-of-run dialog; Yes opens the report, No the folder."""
    core = adsk.core
    if page is None:
        errors.message_box(ui, text, warning=False)
        return None
    answer = ui.messageBox(text + OPEN_CHOICES, 'Fusion Bulk Export',
                           core.MessageBoxButtonTypes.YesNoCancelButtonType,
                           core.MessageBoxIconTypes.InformationIconType)
    target = page if answer == core.DialogResults.DialogYes else (
        output if answer == core.DialogResults.DialogNo else None)
    if target is not None:
        try:
            opener(target)
        except Exception as error:
            errors.message_box(ui, 'Could not open %s (%s). Open it from your file browser.' % (target, error))
    return target


def bar(state):
    """(maximum, value) of the progress bar: projects, as in earlier versions."""
    return max(state['projects'], 1), max(state['project'] - 1, 0)


def export_with_dialogs(app, selection, opener=None):
    ui = app.userInterface
    dialog = ui.createFolderDialog()
    dialog.title = 'Fusion Bulk Export output folder'
    if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    output = os.path.normpath(dialog.folder)  # Fusion returns C:/x on Windows; show C:\x
    return export(app, selection, output, opener)


def export(app, selection, output, opener=None):
    """Export into output with progress, then write the report and show the summary."""
    ui = app.userInterface
    if not has_hubs(app):
        raise errors.no_hubs()
    exporter = open_exporter(output, FusionAdapter(app))
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.show('Fusion Bulk Export', 'Starting…', 0, 1, 0)
    try:
        def update(state):
            progress.maximumValue, progress.progressValue = bar(state)
            progress.message = describe(state)
            adsk.doEvents()

        def cancelled():
            adsk.doEvents()
            return progress.wasCancelled

        def trouble(reason, last_error):
            progress.hide()
            answer = ask_trouble(ui, reason, last_error)
            if answer == 'retry':
                progress.show('Fusion Bulk Export', 'Carrying on…', 0, 1, 0)
            return answer

        try:
            counts = exporter.run(app.data, cancelled, update, selection=selection, on_trouble=trouble)
        except Exception as error:
            known = errors.friendly(error)
            if known is None:
                raise
            raise known from error
    finally:
        progress.hide()
    try:
        page = report.write(exporter, counts)[0]
    except OSError as error:
        exporter.log('could not write the report: ' + str(error))
        page = None
    text = report.summary_text(counts, exporter.run_info, exporter.manifest, output)
    show_summary(ui, text, output, page, opener or report.open_path)
    return counts
