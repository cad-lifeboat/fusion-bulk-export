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
        try:
            forget_package()
            from fusion_bulk_export import autorun
        except Exception:
            answer_job(traceback.format_exc())
            return
        if _busy['by'] == 'button':
            # Fusion processes events during an export: a job may arrive in the middle of one.
            autorun.reject_busy(adsk.core.Application.get(), _HERE / 'autorun.json')
            return
        _busy['by'] = 'job'
        try:
            autorun.run_autorun(adsk.core.Application.get(), _HERE / 'autorun.json')
        except Exception:
            answer_job(traceback.format_exc())  # run_autorun answers itself; this is the last resort
        finally:
            _busy['by'] = None


def answer_job(text):
    """Consume a job the package could not run (it failed to load) and answer it in
    autorun-output beside the add-in, where the launcher looks: a job taken never goes unanswered."""
    try:
        job = _HERE / 'autorun.json'
        if job.is_file():
            job.replace(job.with_name(job.name + '.last'))
        folder = _HERE / 'autorun-output'
        folder.mkdir(parents=True, exist_ok=True)
        lines = [line for line in text.strip().splitlines() if line.strip()]
        result = dict(schema=1, finished=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), errors=[dict(
            stage='autorun', error='The add-in could not run the job: ' + (lines[-1] if lines else 'unknown error'),
            traceback=text)])
        (folder / 'autorun-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass


class Watcher(threading.Thread):
    """Background poll: keeps autorun.alive fresh and signals new autorun.json files."""
    def __init__(self, interval=3):
        super().__init__(daemon=True)
        self.interval = interval
        self.stopped = threading.Event()

    def poll(self):
        # The heartbeat also says whether an export is running: the command-line launcher then
        # replaces no files of this add-in.
        (_HERE / 'autorun.alive').write_text(json.dumps(dict(time=time.time(), busy=_busy['by'])),
                                           encoding='utf-8')
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
                                other_files=_picker.other_files, step=getattr(_picker, 'step', None))
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
                                   (command.incomingFromHTML, FromPage()), (command.destroy, ExportClosed())):
                event.add(handler)
                _handlers.append(handler)
            command.isRepeatable = False
        except Exception:
            report('Fusion Bulk Export command error')


# ---- The results dialog -------------------------------------------------------------------
# A second command, started once the export command has finished, as proven on real Fusion: the
# export's execute handler fires a custom event as its last act and the event's handler calls
# definition.execute(); if the event cannot be fired, the export command's destroy handler starts
# it instead. Each run's results go through these states, in _results['run']:
#   waiting  -> starting (execute() called) -> started (execute() returned True)
#            -> shown (commandCreated built the dialog) -> closed
#   or       -> fallback (the message box: the dialog could not be made, reason logged).
# A run is claimed once, so the dialog and the message box never both show. A check every
# CHECK_SECONDS sees whether Fusion has built the dialog; the message box shows only when it has
# not after PATIENCE_SECONDS since execute() AND after PATIENCE_CHECKS checks were handled. A check
# is a custom event, handled only when Fusion is free to handle events, so a Fusion that is busy
# (after a run of hours, for minutes) counts few checks and is not given up on: a dialog that is
# merely late still shows. (Until 0.3.1 one check after 5 s decided; after a two-hour run Fusion
# took longer than that and the box replaced the dialog.) Everything lives in this file, not in the
# package, so it survives the package being reloaded (forget_package); every handler is kept in
# _handlers until stop().
_RESULTS_ID = 'FusionBulkExportResults'
_RESULTS_EVENT = 'FusionBulkExportShowResults'
_results = {'definition': None, 'event': None, 'run': None, 'view': None, 'last': None, 'runs': 0}
CHECK_SECONDS = 5.0
PATIENCE_SECONDS = 120.0
PATIENCE_CHECKS = 12
_Timer = threading.Timer  # (the tests run the check themselves)
_clock = time.monotonic   # (and move this clock)


def _results_module():
    from fusion_bulk_export import results
    return results


def _short(error):
    """An exception as one line without names (paths and quoted text masked)."""
    try:
        from fusion_bulk_export import errors
        return '%s: %s' % (type(error).__name__, errors.message_of(error))
    except Exception:
        return type(error).__name__


def results_fallback(data, reason):
    """The summary in a plain message box (OK only): the dialog could not be made. The reason
    always goes to results-fallback.log beside the add-in (a line, no names)."""
    trace('results fallback: ' + reason)
    run = _results['run']
    if run is not None and run['data'] is data:
        run['state'] = 'fallback'
    ui = adsk.core.Application.get().userInterface
    try:
        results = _results_module()
        results.log_fallback(_HERE, reason)
        results.fallback(ui, data)
    except Exception as error:
        trace('results fallback box: %s' % error)
        ui.messageBox(data.get('text') or '', 'Fusion Bulk Export')


def show_results(data):
    """At the end of the button's export (inside its execute handler): write this run's page,
    then ask Fusion to start the results dialog once the export command has finished."""
    trace('results')
    if not results_dialog_enabled():
        results_fallback(data, 'results dialog switched off')
        return
    if _results['definition'] is None or _results['event'] is None:
        results_fallback(data, 'results command or event not registered at start-up')
        return
    try:
        page = _results_module().write_page(data)
    except Exception as error:
        results_fallback(data, 'results page not written: ' + _short(error))
        return
    _results['runs'] += 1
    run = dict(data=data, page=page, state='waiting', number=_results['runs'], via='event')
    _results['run'] = run
    trace('results page written, run %d' % run['number'])
    try:
        fired = adsk.core.Application.get().fireCustomEvent(_RESULTS_EVENT, 'show:%d' % run['number'])
    except Exception as error:
        fired = 'raised ' + _short(error)
    trace('results event fired: %s' % (fired,))
    if fired is not True:
        # Not relied on: the export command's destroy handler starts the dialog instead.
        run['via'] = 'destroy'
    start_check(run['number'])


def start_check(number):
    """CHECK_SECONDS from now, check (on Fusion's main thread, through the custom event) that
    the dialog was built; if not, the message box shows."""
    def fire():
        try:
            adsk.core.Application.get().fireCustomEvent(_RESULTS_EVENT, 'check:%d' % number)
        except Exception:
            pass
    try:
        timer = _Timer(CHECK_SECONDS, fire)
        timer.daemon = True
        timer.start()
    except Exception as error:
        trace('results check timer: %s' % error)


def results_definition():
    """The results command's definition: the one made at start, found again by id, or made anew."""
    definitions = adsk.core.Application.get().userInterface.commandDefinitions
    definition = _results['definition']
    try:
        found = definitions.itemById(_RESULTS_ID)
    except Exception as error:
        trace('results itemById: %s' % error)
        found = definition
    if found is not None:
        trace('results definition found')
        return found
    trace('results definition missing: made again')
    return add_results_definition(definitions)


def start_results(trigger):
    """Start the results command for the waiting run (once; later calls do nothing)."""
    run = _results['run']
    trace('results start (%s): %s' % (trigger, run['state'] if run else 'no run'))
    if run is None or run['state'] != 'waiting':
        return
    run['state'] = 'starting'
    run.update(asked=_clock(), checks=0)
    try:
        started = results_definition().execute()
    except Exception as error:
        trace('results execute raised: %s' % error)
        results_fallback(run['data'], 'definition.execute() raised ' + _short(error))
        return
    trace('results execute returned: %s' % (started,))
    if started is False:
        if run['state'] == 'starting':
            results_fallback(run['data'], 'definition.execute() returned False')
        return
    if run['state'] == 'starting':
        run['state'] = 'started'


def check_results(number):
    """A check, CHECK_SECONDS after the last: once the dialog is asked for (execute()), it is
    waited for until PATIENCE_SECONDS have passed and PATIENCE_CHECKS checks were handled; only
    then the message box shows (and a dialog Fusion builds later ends without showing)."""
    run = _results['run']
    if run is None or run['number'] != number:
        return
    trace('results check: %s' % run['state'])
    if run['state'] == 'waiting':
        start_results('check')  # neither the event nor the destroy handler started it
        if run['state'] in ('starting', 'started'):
            start_check(number)  # commandCreated gets its time from now
        return
    if run['state'] in ('starting', 'started'):
        run['checks'] = run.get('checks', 0) + 1
        waited = _clock() - run.get('asked', _clock())
        if waited < PATIENCE_SECONDS or run['checks'] < PATIENCE_CHECKS:
            trace('results dialog not built yet (%d s, check %d): waiting' % (waited, run['checks']))
            start_check(number)
            return
        results_fallback(run['data'], 'commandCreated did not run within %d s after execute() (%d checks)'
                         % (waited, run['checks']))


class ResultsEvent(adsk.core.CustomEventHandler):
    """On Fusion's main thread once it is idle: "show:N" starts the results command (the export
    command has finished by now), "check:N" checks that its dialog was built."""
    def notify(self, args):
        try:
            info = str(getattr(args, 'additionalInfo', '') or '')
            trace('results event handler: %s' % info)
            kind, _, number = info.partition(':')
            run = _results['run']
            if run is None or str(run['number']) != number:
                return
            if kind == 'check':
                check_results(run['number'])
            elif kind == 'show':
                start_results('event')
        except Exception as error:
            trace('results event: %s' % error)


class ExportClosed(adsk.core.CommandEventHandler):
    """The export command ended: start the results dialog here when the custom event could not
    be fired."""
    def notify(self, args):
        try:
            run = _results['run']
            trace('export command destroyed')
            if run is not None and run['state'] == 'waiting' and run['via'] == 'destroy':
                start_results('destroy')
        except Exception as error:
            trace('export destroy: %s' % error)


class ResultsPage(adsk.core.HTMLEventHandler):
    """The results page's buttons: Open report, Open folder, Close (Enter or Escape), and its
    "ready" (logged only). The summary itself is already in the page."""
    def notify(self, args):
        try:
            trace('results page says: %s' % args.action)
            view = _results['view']
            if view is not None:
                args.returnData = view.incoming(args.action, args.data)
                trace('results page answered: %s' % ('ok' if '"ok"' in args.returnData else 'an error'))
        except Exception:
            report('Fusion Bulk Export error')


class ResultsClosed(adsk.core.CommandEventHandler):
    """The results dialog closed: its page is deleted. Never a message box here."""
    def notify(self, args):
        trace('results dialog closed')
        view, _results['view'] = _results['view'], None
        run = _results['run']
        if run is not None and run['state'] == 'shown':
            run['state'] = 'closed'
        try:
            _results_module().remove_pages()
        except Exception as error:
            trace('results pages: %s' % error)


class ResultsCreated(adsk.core.CommandCreatedEventHandler):
    """Builds the results dialog; when that fails, the summary goes to a plain message box and
    the empty command ends without showing a dialog."""
    def notify(self, args):
        trace('results commandCreated')
        command = args.command
        run = _results['run']
        page = None
        if run is not None and run['state'] in ('starting', 'started'):
            data, page = run['data'], run['page']
        elif run is not None and run['state'] in ('waiting', 'fallback'):
            data = None  # not ours to show (a message box is showing, or the export still runs)
        else:
            data = _results['last']  # started some other way (Fusion's command search): the last results
        if data is None:
            trace('results commandCreated: nothing to show')
            try:
                command.isAutoExecute = True  # nothing to show: end without a dialog
            except Exception as error:
                trace('isAutoExecute: %s' % error)
            return
        try:
            results = _results_module()
            if page is None or not Path(page).is_file():
                page = results.write_page(data)
            _results['view'] = results.Results(command, data, page, trace=trace)
            for event, handler in ((command.incomingFromHTML, ResultsPage()), (command.destroy, ResultsClosed())):
                event.add(handler)
                _handlers.append(handler)
        except Exception as error:
            trace('results dialog: %s' % error)
            _results['view'] = None
            try:
                command.isAutoExecute = True  # the half-built dialog is not shown
            except Exception:
                pass
            if run is not None and run['data'] is data:
                results_fallback(data, 'commandCreated raised ' + _short(error))
            else:
                results_fallback(data, 'commandCreated (started again) raised ' + _short(error))
            return
        _results['last'] = data
        if run is not None and run['data'] is data:
            run['state'] = 'shown'
        trace('results dialog built')


def results_dialog_enabled():
    """results.DIALOG_ENABLED, the one switch for the results dialog (False: message box only)."""
    try:
        return bool(_results_module().DIALOG_ENABLED)
    except Exception as error:
        trace('results switch: %s' % error)
        return False


def add_results_definition(definitions):
    definition = definitions.addButtonDefinition(
        _RESULTS_ID, 'Fusion Bulk Export results', 'The result of the last export',
        str(Path(__file__).parent / 'resources'))
    created = ResultsCreated()
    definition.commandCreated.add(created)
    _handlers.append(created)
    _results['definition'] = definition
    trace('results definition created')
    return definition


def add_results():
    """The results command (not on any toolbar) and the custom event that starts it. Without
    them the end-of-run summary is a plain message box. Nothing is added while the results dialog
    is switched off (results.DIALOG_ENABLED). Pages left by an earlier session are deleted."""
    if not results_dialog_enabled():
        trace('results dialog switched off')
        return
    try:
        _results_module().remove_pages()
    except Exception as error:
        trace('results pages: %s' % error)
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
        add_results_definition(definitions)
        try:
            app.unregisterCustomEvent(_RESULTS_EVENT)  # left over by a run that did not stop cleanly
        except Exception:
            pass
        event = app.registerCustomEvent(_RESULTS_EVENT)
        handler = ResultsEvent()
        event.add(handler)
        _handlers.append(handler)
        _results['event'] = event
        trace('results event registered')
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
    try:
        _results_module().remove_pages()
    except Exception as error:
        trace('results pages: %s' % error)
    _results.update(definition=None, event=None, run=None, view=None, last=None)


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
