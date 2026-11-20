"""Fusion add-in entry point (MIT)."""
import json
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


# One export at a time: None, 'button' (the dialog's export) or 'job' (an autorun.json job).
# Kept in this file, not the package, so it survives the package being reloaded.
_busy = {'by': None}


def forget_package():
    """Fusion keeps Python modules loaded between Stop and Run and between jobs; drop ours so
    the next import reads the installed files. Never while an export runs: its code would then
    raise errors of the old modules that the new ones do not recognise. (The synthetic test API
    keeps them: tests patch these modules.)"""
    if getattr(adsk, '_synthetic', False) or _busy['by'] is not None:
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
# The results dialog: a second command, started through its own custom event once the export
# command has finished (a command cannot be started reliably from another one's execute).
_RESULTS_ID = 'FusionBulkExportResults'
_RESULTS_EVENT = 'FusionBulkExportShowResults'
_results = {'definition': None, 'event': None, 'data': None, 'view': None, 'last': None}


class AutorunEvent(adsk.core.CustomEventHandler):
    """Runs a pending autorun.json on Fusion's main thread."""
    def notify(self, args):
        if _busy['by'] == 'job' or not (_HERE / 'autorun.json').is_file():
            return
        # Each job uses the add-in package as currently installed (but no reload while the
        # button's export runs: see forget_package).
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
        # The heartbeat also says whether an export is running: the command-line launcher then
        # replaces no files of this add-in.
        (_HERE / 'autorun.alive').write_text(json.dumps(dict(time=time.time(), busy=_busy['by'])))
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
        errors.show(ui, title, error, _LOG_DIR)
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
    """Export stays greyed out until there is a folder and Everything or something ticked."""
    def notify(self, args):
        try:
            args.areInputsValid = _picker is None or _picker.ready()
        except Exception:
            report('Fusion Bulk Export error')


class Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        try:
            if _picker is None:
                return
            selection = _picker.selection()
            if selection is None:
                app.userInterface.messageBox('Tick at least one project or folder, or choose Everything.',
                                             'Fusion Bulk Export')
                return
            if not _picker.output:
                app.userInterface.messageBox('Choose where to save the export (Browse…).', 'Fusion Bulk Export')
                return
            if _busy['by'] is not None:
                app.userInterface.messageBox(
                    'An export started from the command line is running in Fusion. '
                    'Wait for it to finish (or stop it with "autorun.py stop"), then export again.',
                    'Fusion Bulk Export')
                return
            _picker.remember()
            _busy['by'] = 'button'
            try:
                _code['export'](app, selection, _picker.output, show=show_results,
                                other_files=_picker.other_files)
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
            from fusion_bulk_export.command import export
            from fusion_bulk_export.picker import DIALOG_MINIMUM, DIALOG_SIZE, Picker
            _code.update(export=export)
            command = args.command
            command.okButtonText = 'Export'
            # Wide and tall enough for the tree of projects (Fusion's default cuts names off).
            command.setDialogInitialSize(*DIALOG_SIZE)
            command.setDialogMinimumSize(*DIALOG_MINIMUM)
            _picker = Picker(adsk.core.Application.get(), command.commandInputs, trace)
            for event, handler in ((command.execute, Execute()), (command.inputChanged, InputChanged()),
                                   (command.validateInputs, Validate()),
                                   (command.incomingFromHTML, FromPage())):
                event.add(handler)
                _handlers.append(handler)
            command.isRepeatable = False
        except Exception:
            report('Fusion Bulk Export command error')


def results_fallback(data):
    """The summary in a plain message box (OK only): when the results dialog cannot be shown."""
    _results['data'] = None
    ui = adsk.core.Application.get().userInterface
    try:
        from fusion_bulk_export import results
        results.fallback(ui, data)
    except Exception:
        ui.messageBox(data.get('text') or '', 'Fusion Bulk Export')


def show_results(data):
    """At the end of the button's export: keep the summary and ask Fusion, through a custom
    event, to open the results dialog once the export command has finished."""
    trace('results')
    if not results_dialog_enabled() or _results['definition'] is None or _results['event'] is None:
        results_fallback(data)
        return
    _results['data'] = data
    try:
        fired = adsk.core.Application.get().fireCustomEvent(_RESULTS_EVENT, '')
    except Exception as error:
        trace('fireCustomEvent: %s' % error)
        fired = False
    if fired is False:
        results_fallback(data)


class ResultsEvent(adsk.core.CustomEventHandler):
    """Starts the results command (the export command has finished by now)."""
    def notify(self, args):
        data = _results['data']
        if data is None:
            return
        try:
            _results['definition'].execute()
        except Exception as error:
            trace('results execute: %s' % error)
            results_fallback(data)


class ResultsPage(adsk.core.HTMLEventHandler):
    """Requests from the results page: its data, Open report, Open folder, Close."""
    def notify(self, args):
        try:
            if _results['view']:
                args.returnData = _results['view'].incoming(args.action, args.data)
        except Exception:
            report('Fusion Bulk Export error')


class ResultsClosed(adsk.core.CommandEventHandler):
    """The results dialog closed; if its page never showed the results, a message box does."""
    def notify(self, args):
        view, _results['view'] = _results['view'], None
        try:
            from fusion_bulk_export import results
            results.closed(adsk.core.Application.get().userInterface, view)
        except Exception as error:
            trace('results closed: %s' % error)


class ResultsCreated(adsk.core.CommandCreatedEventHandler):
    """Builds the results dialog; when that fails, the summary goes to a plain message box and
    the empty command ends without showing a dialog."""
    def notify(self, args):
        # Started some other way (Fusion's command search finds it too): the last results.
        data = _results['data'] or _results['last']
        _results['data'] = None
        command = args.command
        if data is None:
            try:
                command.isAutoExecute = True  # nothing to show: end without a dialog
            except Exception as error:
                trace('isAutoExecute: %s' % error)
            return
        _results['last'] = data
        try:
            from fusion_bulk_export.results import Results
            _results['view'] = Results(command, data, trace=trace)
            page = ResultsPage()
            command.incomingFromHTML.add(page)
            _handlers.append(page)
        except Exception as error:
            trace('results dialog: %s' % error)
            _results['view'] = None
            try:
                command.isAutoExecute = True
            except Exception:
                pass
            results_fallback(data)
            return
        try:
            closed = ResultsClosed()
            command.destroy.add(closed)
            _handlers.append(closed)
        except Exception as error:
            trace('results destroy: %s' % error)


def results_dialog_enabled():
    """results.DIALOG_ENABLED, the one switch for the results dialog (False: message box only)."""
    try:
        from fusion_bulk_export import results
        return bool(results.DIALOG_ENABLED)
    except Exception as error:
        trace('results switch: %s' % error)
        return False


def add_results():
    """The results command (not on any toolbar) and the custom event that starts it. Without
    them the end-of-run summary is a plain message box. Nothing is added while the results dialog
    is switched off (results.DIALOG_ENABLED)."""
    if not results_dialog_enabled():
        trace('results dialog switched off')
        return
    app = adsk.core.Application.get()
    try:
        definitions = app.userInterface.commandDefinitions
        try:
            # Our own command left over by a run that did not stop cleanly.
            definition = definitions.itemById(_RESULTS_ID)
            if definition:
                definition.deleteMe()
        except Exception as error:
            trace('itemById: %s' % error)
        definition = definitions.addButtonDefinition(
            _RESULTS_ID, 'Fusion Bulk Export results', 'The result of the last export',
            str(Path(__file__).parent / 'resources'))
        created = ResultsCreated()
        definition.commandCreated.add(created)
        _handlers.append(created)
        _results['definition'] = definition
        event = app.registerCustomEvent(_RESULTS_EVENT)
        handler = ResultsEvent()
        event.add(handler)
        _handlers.append(handler)
        _results['event'] = event
    except Exception as error:
        trace('results command: %s' % error)


def remove_results():
    app = adsk.core.Application.get()
    try:
        if _results['event'] is not None:
            app.unregisterCustomEvent(_RESULTS_EVENT)
        definition = _results['definition']
        if definition is not None:
            definition.deleteMe()
    except Exception as error:
        trace('remove results: %s' % error)
    _results.update(definition=None, event=None, data=None, view=None, last=None)


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
    add_results()
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
    remove_results()
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
