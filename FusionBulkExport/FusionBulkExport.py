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
    # Load the package once now, so a broken installation is reported at start-up.
    import fusion_bulk_export.autorun  # noqa: F401
    import fusion_bulk_export.command  # noqa: F401
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


# One export at a time: None, 'button' (the dialog's export) or 'job' (an autorun.json job).
# Kept in this file, not the package, so it survives the package being reloaded.
_busy = {'by': None}


class AutorunEvent(adsk.core.CustomEventHandler):
    """Runs a pending autorun.json on Fusion's main thread."""
    def notify(self, args):
        if _busy['by'] == 'job' or not (_HERE / 'autorun.json').is_file():
            return
        # Each job uses the add-in package as currently installed.
        forget_package()
        from fusion_bulk_export import autorun
        if _busy['by'] == 'button':
            # Fusion processes events during an export: a job may arrive in the middle of one.
            autorun.reject_busy(adsk.core.Application.get(), _HERE / 'autorun.json')
            return
        _busy['by'] = 'job'
        try:
            autorun.run_autorun(adsk.core.Application.get(), _HERE / 'autorun.json')
        finally:
            _busy['by'] = None


class Watcher(threading.Thread):
    """Background poll: keeps autorun.alive fresh and signals new autorun.json files."""
    def __init__(self, interval=3):
        super().__init__(daemon=True)
        self.interval = interval
        self.stopped = threading.Event()

    def poll(self):
        (_HERE / 'autorun.alive').write_text(str(time.time()))
        if (_HERE / 'autorun.json').is_file() and _busy['by'] != 'job':
            adsk.core.Application.get().fireCustomEvent(_EVENT, '')

    def run(self):
        while not self.stopped.is_set():
            try:
                self.poll()
            except Exception:
                pass
            self.stopped.wait(self.interval)


def start_watching():
    """Run a pending autorun job now and whenever one appears (alongside the button)."""
    global _watcher, _custom_event
    app = adsk.core.Application.get()
    _custom_event = app.registerCustomEvent(_EVENT)
    handler = AutorunEvent()
    _custom_event.add(handler)
    _handlers.append(handler)
    _watcher = Watcher()
    _watcher.start()
    handler.notify(None)


_picker = None
_code = {}  # the package's routines as loaded when the dialog opened


# errors.log goes beside the add-in (the synthetic test API sets its own folder or none).
_LOG_DIR = None if getattr(adsk, '_synthetic', False) else _DIR


def report(title):
    """Tell the user about the current exception: expected problems in plain words, anything
    else logged to errors.log with the details in a copyable box."""
    error = sys.exc_info()[1]
    detail = traceback.format_exc()
    ui = adsk.core.Application.get().userInterface
    try:
        from fusion_bulk_export import errors
        errors.show(ui, title, error, detail, _LOG_DIR)
    except Exception:
        ui.messageBox(detail, title)  # the package itself is broken: show the raw details


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


class FromPage(adsk.core.HTMLEventHandler):
    """Requests from the tree page in the dialog; the reply goes back as returnData."""
    def notify(self, args):
        try:
            if _picker:
                args.returnData = _picker.incoming(args.action, args.data)
        except Exception:
            report('Fusion Bulk Export error')


class Validate(adsk.core.ValidateInputsEventHandler):
    """Export… stays greyed out until Everything is chosen or something is ticked."""
    def notify(self, args):
        try:
            args.areInputsValid = _picker is None or _picker.selection() is not None
        except Exception:
            report('Fusion Bulk Export error')


class Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        try:
            selection = _picker.selection() if _picker else None
            if _picker and selection is None:
                app.userInterface.messageBox('Tick at least one project or folder, or choose Everything.',
                                             'Fusion Bulk Export')
                return
            if _busy['by'] is not None:
                app.userInterface.messageBox(
                    'An export started from the command line is running in Fusion. '
                    'Wait for it to finish (or stop it with "autorun.py stop"), then export again.',
                    'Fusion Bulk Export')
                return
            _busy['by'] = 'button'
            try:
                _code['export_with_dialogs'](app, selection)
            finally:
                _busy['by'] = None
        except Exception:
            report('Fusion Bulk Export error')


class Created(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _picker
        try:
            # Each dialog uses the add-in code as currently installed, without a restart.
            forget_package()
            from fusion_bulk_export.command import export_with_dialogs
            from fusion_bulk_export.picker import Picker
            _code.update(export_with_dialogs=export_with_dialogs)
            command = args.command
            command.okButtonText = 'Export…'
            # Wide enough for project and folder names (Fusion's default cuts labels off).
            command.setDialogInitialSize(460, 520)
            command.setDialogMinimumSize(380, 160)
            _picker = Picker(adsk.core.Application.get(), command.commandInputs, trace)
            for event, handler in ((command.execute, Execute()), (command.inputChanged, InputChanged()),
                                   (command.validateInputs, Validate()),
                                   (command.incomingFromHTML, FromPage())):
                event.add(handler)
                _handlers.append(handler)
            command.isRepeatable = False
        except Exception:
            report('Fusion Bulk Export command error')


def startup_error(unattended):
    """A start-up failure: always written to startup-error.txt (the command-line launcher shows
    it); shown in a dialog only when no job is waiting, since a dialog would block the job."""
    try:
        (_HERE / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    except OSError:
        pass
    if not unattended:
        report('Fusion Bulk Export startup error')


def run(context):
    """The button is always there, and autorun.json jobs are always watched for."""
    global _control, _definition
    unattended = (_HERE / 'autorun.json').is_file()
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
        startup_error(unattended)
    try:
        start_watching()
    except Exception:
        startup_error(unattended)
        if unattended:
            raise


def stop(context):
    global _control, _definition, _watcher, _custom_event, _picker
    _picker = None
    _busy['by'] = None
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
