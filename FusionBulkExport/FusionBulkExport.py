"""Fusion add-in entry point (MIT)."""
from pathlib import Path
import traceback
import adsk.core
import adsk.fusion
from fusion_bulk_export.core import Exporter
from fusion_bulk_export.fusion import FusionAdapter

_handlers = []
_control = None
_definition = None
_ID = 'FusionBulkExport'


class Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        progress = None
        try:
            dialog = ui.createFolderDialog()
            dialog.title = 'Fusion Bulk Export output folder'
            if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
                return
            output = dialog.folder
            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show('Fusion Bulk Export', 'Discovering designs…', 0, 1, 0)

            def update(index, total, name):
                progress.maximumValue = max(total, 1)
                progress.progressValue = index
                progress.message = name
                adsk.doEvents()

            def cancelled():
                adsk.doEvents()
                return progress.wasCancelled

            exporter = Exporter(output, FusionAdapter(app))
            counts = exporter.run(app.data, cancelled, update)
            progress.hide()
            summary = ('Cancelled.\n' if exporter.run_info['cancelled'] else 'Finished.\n')
            summary += 'Done: {done}; failed: {failed}; skipped/verified: {skipped}\n'.format(**counts)
            ui.inputBox(summary + '\nOutput folder (select and copy):',
                        'Fusion Bulk Export', output)
        except Exception:
            ui.messageBox(traceback.format_exc(), 'Fusion Bulk Export error')
        finally:
            if progress:
                progress.hide()


class Created(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            handler = Execute()
            args.command.execute.add(handler)
            _handlers.append(handler)
            args.command.isRepeatable = False
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), 'Fusion Bulk Export command error')


def run(context):
    global _control, _definition
    config = Path(__file__).parent / 'selftest.json'
    if config.is_file():
        from fusion_bulk_export.selftest import run_selftest
        run_selftest(adsk.core.Application.get(), config)
        return
    ui = adsk.core.Application.get().userInterface
    try:
        _definition = ui.commandDefinitions.addButtonDefinition(
            _ID, 'Fusion Bulk Export', 'Keep local copies of your Fusion designs',
            str(Path(__file__).parent / 'resources'))
        handler = Created()
        _definition.commandCreated.add(handler)
        _handlers.append(handler)
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        _control = panel.controls.addCommand(_definition)
    except Exception:
        ui.messageBox(traceback.format_exc(), 'Fusion Bulk Export startup error')


def stop(context):
    global _control, _definition
    try:
        if _control:
            _control.deleteMe()
        if _definition:
            _definition.deleteMe()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            traceback.format_exc(), 'Fusion Bulk Export shutdown error')
    finally:
        _control = None
        _definition = None
        _handlers.clear()
