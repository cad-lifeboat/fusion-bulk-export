"""Fusion add-in entry point (MIT)."""
import os
import sys
from pathlib import Path
import threading
import time
import traceback
import adsk.core
import adsk.fusion

# Fusion does not put an add-in's own folder on sys.path; the package lives beside this file.
_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)


def forget_package():
    """Fusion keeps Python modules loaded between Stop and Run and between jobs; drop ours so
    the next import reads the installed files. (The synthetic test API keeps them: tests
    patch these modules.)"""
    if getattr(adsk, '_synthetic', False):
        return
    for name in [n for n in sys.modules if n == 'fusion_bulk_export' or n.startswith('fusion_bulk_export.')]:
        del sys.modules[name]


forget_package()
try:
    from fusion_bulk_export.core import Exporter
    from fusion_bulk_export.fusion import FusionAdapter
except Exception:
    with open(os.path.join(_DIR, 'startup-error.txt'), 'w', encoding='utf-8') as _stream:
        _stream.write(traceback.format_exc())
    raise

_handlers = []
_control = None
_definition = None
_ID = 'FusionBulkExport'
_EVENT = 'FusionBulkExportAutorun'
_HERE = Path(__file__).parent
_watcher = None
_custom_event = None


class AutorunEvent(adsk.core.CustomEventHandler):
    """Runs a pending autorun.json on Fusion's main thread."""
    busy = False

    def notify(self, args):
        if AutorunEvent.busy or not (_HERE / 'autorun.json').is_file():
            return
        AutorunEvent.busy = True
        try:
            # Each job uses the add-in package as currently installed.
            forget_package()
            from fusion_bulk_export.autorun import run_autorun
            run_autorun(adsk.core.Application.get(), _HERE / 'autorun.json')
        finally:
            AutorunEvent.busy = False


class Watcher(threading.Thread):
    """Background poll: keeps autorun.alive fresh and signals new autorun.json files."""
    def __init__(self, interval=3):
        super().__init__(daemon=True)
        self.interval = interval
        self.stopped = threading.Event()

    def poll(self):
        (_HERE / 'autorun.alive').write_text(str(time.time()))
        if (_HERE / 'autorun.json').is_file() and not AutorunEvent.busy:
            adsk.core.Application.get().fireCustomEvent(_EVENT, '')

    def run(self):
        while not self.stopped.is_set():
            try:
                self.poll()
            except Exception:
                pass
            self.stopped.wait(self.interval)


def start_watching():
    """Unattended mode: no command UI; run autorun jobs now and whenever one appears."""
    global _watcher, _custom_event
    app = adsk.core.Application.get()
    _custom_event = app.registerCustomEvent(_EVENT)
    handler = AutorunEvent()
    _custom_event.add(handler)
    _handlers.append(handler)
    handler.notify(None)
    _watcher = Watcher()
    _watcher.start()


_picker = None
_code = dict(Exporter=Exporter, FusionAdapter=FusionAdapter)


def report(title):
    """Show the current exception and append it to errors.log beside the add-in."""
    detail = traceback.format_exc()
    try:
        with open(os.path.join(_DIR, 'errors.log'), 'a', encoding='utf-8') as stream:
            stream.write('%s %s\n%s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), title, detail))
    except OSError:
        pass
    adsk.core.Application.get().userInterface.messageBox(detail, title)


def trace(message):
    """With a file named "trace" beside the add-in, log dialog events to trace.log."""
    if os.path.exists(os.path.join(_DIR, 'trace')):
        with open(os.path.join(_DIR, 'trace.log'), 'a', encoding='utf-8') as stream:
            stream.write('%s %s\n' % (time.strftime('%H:%M:%S'), message))


class InputChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            trace('inputChanged ' + str(args.input.id))
            if _picker:
                _picker.changed(args.input)
        except Exception:
            report('Fusion Bulk Export error')


class Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        progress = None
        try:
            selection = _picker.selection() if _picker else None
            if _picker and selection is None:
                ui.messageBox('Tick at least one project or folder, or choose Everything.',
                              'Fusion Bulk Export')
                return
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

            exporter = _code['Exporter'](output, _code['FusionAdapter'](app))
            counts = exporter.run(app.data, cancelled, update, selection=selection)
            progress.hide()
            summary = ('Cancelled.\n' if exporter.run_info['cancelled'] else 'Finished.\n')
            summary += 'Done: {done}; failed: {failed}; skipped/verified: {skipped}\n'.format(**counts)
            ui.inputBox(summary + '\nOutput folder (select and copy):',
                        'Fusion Bulk Export', output)
        except Exception:
            report('Fusion Bulk Export error')
        finally:
            if progress:
                progress.hide()


class Created(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _picker
        try:
            # Each dialog uses the add-in code as currently installed, without a restart.
            forget_package()
            from fusion_bulk_export.core import Exporter as fresh_exporter
            from fusion_bulk_export.fusion import FusionAdapter as fresh_adapter
            from fusion_bulk_export.picker import Picker
            _code.update(Exporter=fresh_exporter, FusionAdapter=fresh_adapter)
            command = args.command
            command.okButtonText = 'Export…'
            # Wide enough for project and folder names (Fusion's default cuts labels off).
            command.setDialogInitialSize(460, 420)
            command.setDialogMinimumSize(380, 160)
            _picker = Picker(adsk.core.Application.get(), command.commandInputs, trace)
            for event, handler in ((command.execute, Execute()), (command.inputChanged, InputChanged())):
                event.add(handler)
                _handlers.append(handler)
            command.isRepeatable = False
        except Exception:
            report('Fusion Bulk Export command error')


def run(context):
    global _control, _definition
    if (_HERE / 'autorun.json').is_file() or (_HERE / 'autorun.watch').is_file():
        try:
            start_watching()
        except Exception:
            # Unattended: no dialog, but leave the reason where the launcher can show it.
            (_HERE / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            raise
        return
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
        # Show the button on the toolbar, not only inside the ADD-INS menu.
        _control.isPromoted = True
        _control.isPromotedByDefault = True
    except Exception:
        report('Fusion Bulk Export startup error')


def stop(context):
    global _control, _definition, _watcher, _custom_event, _picker
    _picker = None
    if _watcher:
        _watcher.stopped.set()
        _watcher = None
    if _custom_event:
        adsk.core.Application.get().unregisterCustomEvent(_EVENT)
        _custom_event = None
    try:
        if _control:
            _control.deleteMe()
        if _definition:
            _definition.deleteMe()
    except Exception:
        report('Fusion Bulk Export shutdown error')
    finally:
        _control = None
        _definition = None
        _handlers.clear()
