"""The button's export: folder dialog, progress dialog, summary. Lives in the package (not the
entry file) so a reinstall takes effect at the next dialog without restarting the add-in."""
import os

import adsk
import adsk.core

from .core import Exporter
from .fusion import FusionAdapter


def describe(state):
    """Progress text: the bar counts projects; the text says where it is and what it is doing."""
    where = ' / '.join([state['project_name'] or ''] + state['path'])
    head = 'Project %d of %d' % (state['project'], state['projects'])
    if state['phase'] == 'listing':
        text = '%s, listing: %s\n%d design(s) so far' % (head, where, state['design'])
    else:
        text = '%s, design %d: %s\n%s' % (head, state['design'], state['name'], where)
    return text.replace('%', '%%')  # % starts a placeholder in Fusion's progress text


def export_with_dialogs(app, selection):
    ui = app.userInterface
    dialog = ui.createFolderDialog()
    dialog.title = 'Fusion Bulk Export output folder'
    if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    output = os.path.normpath(dialog.folder)  # Fusion returns C:/x on Windows; show C:\x
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.show('Fusion Bulk Export', 'Starting…', 0, 1, 0)
    try:
        def update(state):
            progress.maximumValue = max(state['projects'], 1)
            progress.progressValue = max(state['project'] - 1, 0)
            progress.message = describe(state)
            adsk.doEvents()

        def cancelled():
            adsk.doEvents()
            return progress.wasCancelled

        exporter = Exporter(output, FusionAdapter(app))
        counts = exporter.run(app.data, cancelled, update, selection=selection)
        progress.hide()
        summary = ('Cancelled.\n' if exporter.run_info['cancelled'] else 'Finished.\n')
        summary += 'Done: {done}; failed: {failed}; skipped/verified: {skipped}\n'.format(**counts)
        ui.inputBox(summary + '\nOutput folder (select and copy):', 'Fusion Bulk Export', output)
        return counts
    finally:
        progress.hide()
