"""The button's export: progress dialog, report, summary. Lives in the package (not the
entry file) so a reinstall takes effect at the next dialog without restarting the add-in."""

import adsk
import adsk.core

from . import errors, report
from .core import Exporter
from .fusion import FusionAdapter
from .words import plural


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


STEPS = 1000  # bar resolution within one project


def duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return '%d s' % seconds
    if seconds < 3600:
        return '%d min' % (seconds // 60)
    return '%d h %02d min' % (seconds // 3600, seconds % 3600 // 60)


def describe(state):
    """Progress text: where it is, what it is doing, and time, rate and failures so far."""
    where = ' / '.join([state['project_name'] or ''] + list(state['path']))
    head = 'Project %d of %d' % (state['project'], state['projects'])
    if state['phase'] == 'listing':
        text = '%s, listing: %s\n%s so far' % (head, where, plural(state['design'], 'item'))
    else:
        what = 'file' if state.get('kind') == 'file' else 'design'
        text = '%s, %s %d: %s\n%s' % (head, what, state['design'], state['name'], where)

    facts = [duration(state.get('elapsed'))]
    if state.get('seconds_per_design') is not None:
        facts.append('%.1f s/design' % state['seconds_per_design'])
    facts.append('%d failed' % (state.get('counts') or {}).get('failed', 0))
    text += '\n' + ' · '.join(facts)
    return text.replace('%', '%%')  # % starts a placeholder in Fusion's progress text


def bar(state):
    """(maximum, value) of the progress bar. Each project is one slice; within it the bar
    moves through the current folder when its file count is known, so a single-project
    export moves too."""
    projects = max(state['projects'], 1)
    done = 0.0
    files = state.get('folder_files')
    if state['phase'] == 'design' and files:
        done = min(max(state.get('folder_item', 1) - 1, 0) / float(files), 1.0)
    value = max(state['project'] - 1, 0) * STEPS + int(done * STEPS)
    return projects * STEPS, min(value, projects * STEPS)


def ask(ui, text, buttons, icon):
    """messageBox with buttons (a MessageBoxButtonTypes member: OKCancel, YesNo, YesNoCancel) and,
    where this Fusion offers it, an icon. Only results that exist (DialogOK, DialogCancel,
    DialogYes, DialogNo) are relied on."""
    core = adsk.core
    shown = getattr(getattr(core, 'MessageBoxIconTypes', None), icon, None)
    kind = getattr(core.MessageBoxButtonTypes, buttons)
    if shown is None:
        return ui.messageBox(text, 'Fusion Bulk Export', kind)
    return ui.messageBox(text, 'Fusion Bulk Export', kind, shown)


def trouble_text(reason, last_error):
    return ('The export has paused: %s\nLast error: %s\n\nEverything exported so far is kept. Try again now?'
            % (reason, last_error or 'none'))


def ask_trouble(ui, reason, last_error):
    """The run cannot go on (disk full, folder gone, designs failing in a row): try again or stop.
    Asked during the export, so only a message box will do. OK / Cancel under a question, not
    Retry / Cancel: Fusion's DialogResults has no Retry result to compare with."""
    answer = ask(ui, trouble_text(reason, last_error), 'OKCancelButtonType', 'WarningIconType')
    return 'retry' if answer == adsk.core.DialogResults.DialogOK else 'stop'


def show_results(ui, data):
    """Without a results dialog (a caller that passes none): the summary in a message box."""
    from . import results
    results.fallback(ui, data)


def export(app, selection, output, show=None, other_files=True, drawings=None, linked_copies=None, step=None):
    """Export into output with progress, then write the report and hand the summary to show
    (report.summary() data plus its plain "text"; the add-in shows it in the results dialog).
    other_files: also save the files that are not designs (the dialog's checkbox). step: also
    save each design's STEP (the dialog's STEP box); None takes the remembered choice (an entry
    file loaded before an update does not pass it). drawings and
    linked_copies are ignored: drawings are never exported, and the copies with linked parts are
    a testing-only experiment switched on in Fusion's environment (core.LINKED_COPIES_ENV), never
    from the dialog (both are still accepted from an entry file loaded before an update, until
    Fusion restarts the add-in)."""
    ui = app.userInterface
    if step is None:
        from . import settings
        step = settings.load().get('step') is not False
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
            counts = exporter.run(app.data, cancelled, update, selection=selection, on_trouble=trouble,
                                  other_files=other_files, step=step)
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
    # The folder as the export used it (absolute, never resolved), so "Folder:", "Report:" and
    # Open folder name the same path however the remembered folder was written.
    data = report.summary(counts, exporter.run_info, exporter.manifest, exporter.root, page)
    data['text'] = report.text_of(data)
    (show or (lambda summary: show_results(ui, summary)))(data)
    return counts
