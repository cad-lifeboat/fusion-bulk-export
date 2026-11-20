"""Synthetic command lifecycle and output-folder dialog checks."""
import importlib
import json
import os
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import LinkedTemporaryDirectory, fake_adsk as fake

fake.install()


class Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)


class UIBase(unittest.TestCase):
    results_dialog = True  # results.DIALOG_ENABLED during the test

    def setUp(self):
        import adsk
        self.adsk = adsk
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        # Reached through a link, as macOS and Windows temporary folders are: the add-in keeps
        # the path it was given, never the resolved one.
        self.temp = LinkedTemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.folder = NS(title='', folder=self.temp.name, showDialog=lambda: adsk.core.DialogResults.DialogOK)
        # Remembered settings live in a temporary file here; the last folder is the test's folder.
        home = LinkedTemporaryDirectory(prefix='home-')
        self.addCleanup(home.cleanup)
        self.settings = Path(home.name) / 'settings.json'
        self.settings.write_text(json.dumps(dict(output=self.temp.name)))
        environment = patch.dict('os.environ', FUSION_BULK_EXPORT_SETTINGS=str(self.settings))
        environment.start()
        self.addCleanup(environment.stop)
        self.progress = NS(isCancelButtonShown=False, wasCancelled=False,
                           show=lambda *args: None, hide=lambda: None)
        self.errors, self.details, self.troubles, self.opened, self.plain = [], [], [], [], []
        self.trouble_answer = adsk.core.DialogResults.DialogCancel  # to try again (OK) / stop
        buttons = adsk.core.MessageBoxButtonTypes
        icons = adsk.core.MessageBoxIconTypes

        def message_box(*args):
            if len(args) > 2 and args[2] == buttons.OKCancelButtonType:
                self.troubles.append(args)
                return self.trouble_answer
            if len(args) > 3 and args[2] == buttons.OKButtonType and args[3] == icons.InformationIconType:
                self.plain.append(args)  # the results as a message box (the fallback)
                return adsk.core.DialogResults.DialogOK
            self.errors.append(args)
            return adsk.core.DialogResults.DialogOK
        self.control = NS(deleteMe=lambda: None)
        self.definitions = fake.CommandDefinitions()
        self.custom = fake.CustomEvents()
        self.ui = NS(createFolderDialog=lambda: self.folder,
                     createProgressDialog=lambda: self.progress,
                     inputBox=lambda *args: self.details.append(args),
                     messageBox=message_box,
                     commandDefinitions=self.definitions,
                     allToolbarPanels=NS(itemById=lambda name: NS(controls=NS(addCommand=lambda definition: self.control))))
        self.file = fake.File()
        self.app = NS(userInterface=self.ui, documents=fake.Documents(), data=fake.data([self.file]),
                      registerCustomEvent=self.custom.register, unregisterCustomEvent=self.custom.unregister,
                      fireCustomEvent=self.custom.fire)
        adsk.core.Application = NS(get=lambda: self.app)
        adsk.doEvents = lambda: None
        self.module = importlib.import_module('FusionBulkExport')
        # The add-in's own folder: job files and the heartbeat go to a temporary one here.
        addin = tempfile.TemporaryDirectory(prefix='addin-')
        self.addCleanup(addin.cleanup)
        self.addin = Path(addin.name)
        for target, value in ((self.module, '_HERE'), (self.module.Watcher, 'start')):
            patcher = patch.object(target, value, self.addin if value == '_HERE' else (lambda watcher: None))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.module.stop, None)
        # These tests drive the results dialog; it is switched off by default (see
        # ResultsSwitchTests), so it is switched on here.
        switch = patch('fusion_bulk_export.results.DIALOG_ENABLED', self.results_dialog)
        switch.start()
        self.addCleanup(switch.stop)
        opener = patch('fusion_bulk_export.report.open_path', side_effect=self.opened.append)
        opener.start()
        self.addCleanup(opener.stop)
        # The check CHECK_SECONDS after an export runs when a test says so (run_checks()).
        self.timers = []
        tests = self

        class Timer:
            def __init__(self, seconds, function):
                self.seconds, self.function, self.daemon = seconds, function, False

            def start(self):
                tests.timers.append(self)
        timer = patch.object(self.module, '_Timer', Timer)
        timer.start()
        self.addCleanup(timer.stop)
        # The add-in's clock: a timer firing moves it on by the timer's seconds.
        self.now = [0.0]
        clock = patch.object(self.module, '_clock', lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    @property
    def definition(self):
        return self.definitions.itemById('FusionBulkExport')

    def open_dialog(self):
        if self.definition is None:
            self.module.run(None)
        command = fake.Command()
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        return command

    def export(self):
        """Open the dialog and press Export: Fusion runs the command's execute, then ends it."""
        command = self.open_dialog()
        command.execute.handlers[0].notify(None)
        self.end(command)

    @staticmethod
    def end(command):
        """Fusion ends a command after its execute: its destroy handlers run."""
        command.destroy.notify(NS())

    def results(self):
        """Fusion goes idle after the export command: the custom event starts the results
        dialog. Returns (its command, the summary data it shows, the summary's plain text)."""
        self.custom.pump()
        command = self.definitions.itemById('FusionBulkExportResults').commands[-1]
        data = self.module._results['view'].data
        return command, data, data['text']

    def run_checks(self):
        """CHECK_SECONDS later: the timers fire their custom event, which Fusion then handles."""
        while self.timers:
            timer = self.timers.pop(0)
            self.now[0] += timer.seconds
            timer.function()
            self.custom.pump()

    @staticmethod
    def page_file(command):
        """The page file the results dialog shows (from its URL)."""
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        return Path(url2pathname(urlparse(command.commandInputs.find('fbx_results').htmlFileURL).path))

    def assertFolderShown(self, inputs, path):
        """The dialog shows the output folder whole, or with its middle cut when long (the full
        path is its tooltip): temporary folders on macOS and Windows are long enough to be cut."""
        from fusion_bulk_export.picker import shown_path
        folder = inputs.find('fbx_folder')
        self.assertEqual((folder.text, folder.tooltip), (shown_path(str(path)), str(path)))

    def fallback_log(self):
        path = self.addin / 'results-fallback.log'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def change(self, command, control):
        command.inputChanged.handlers[0].notify(NS(input=control))
        self.assertEqual(self.errors, [])

    def picker_data(self):
        """Hub with two projects; the first has folders Top/Sub and Other."""
        self.files = {i: fake.File(identity=i) for i in ('root', 'top', 'deep', 'other', 'second')}
        sub = fake.folder('Sub', files=[self.files['deep']])
        self.app.data = fake.data([self.files['root']], [fake.folder('Top', files=[self.files['top']], folders=[sub]),
                                                          fake.folder('Other', files=[self.files['other']])])
        self.app.data.dataHubs.item(0).dataProjects.items.append(
            NS(id='project-2', name='Two', rootFolder=fake.folder(files=[self.files['second']])))

    def exported(self):
        manifest = json.loads((Path(self.temp.name) / 'manifest.json').read_text(encoding='utf-8'))
        return sorted(e['id'] for e in manifest['entries'] if e['status'] == 'done')

    def page(self, command, action, data=None):
        """What the tree page does: send a request and read the reply."""
        args = NS(action=action, data=json.dumps(data or {}), returnData='')
        command.incomingFromHTML.handlers[0].notify(args)
        self.assertEqual(self.errors, [])
        return json.loads(args.returnData)

    def choose(self, command):
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        return inputs

    def valid(self, command):
        args = NS(areInputsValid=True)
        command.validateInputs.handlers[0].notify(args)
        return args.areInputsValid

class UItests(UIBase):
    def test_dialog_defaults_to_everything(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        self.assertEqual(inputs.find('fbx_mode').selectedItem.name, 'Everything')
        self.assertFalse(inputs.find('fbx_tree').isVisible)
        self.assertIn('one project or folder', inputs.find('fbx_tip').text)
        self.assertTrue(inputs.find('fbx_tree').htmlFileURL.endswith('/ui/tree.html'))
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'other', 'root', 'second', 'top'])

    def test_dialog_shows_the_version(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'FusionBulkExport' /
                               'FusionBulkExport.manifest').read_text(encoding='utf-8'))
        inputs = self.open_dialog().commandInputs
        self.assertEqual(inputs.find('fbx_version').text, 'Fusion Bulk Export ' + manifest['version'])
        from fusion_bulk_export.picker import version
        self.assertIsNone(version(Path(self.temp.name) / 'missing.manifest'))

    def test_tree_lists_hubs_projects_and_folders_on_demand(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.assertTrue(inputs.find('fbx_tree').isVisible)
        hubs = self.page(command, 'hubs')['hubs']
        self.assertEqual([(p['id'], p['name']) for p in hubs[0]['projects']],
                         [('project-1', 'Project'), ('project-2', 'Two')])
        reply = self.page(command, 'folders', dict(project='project-1', path=[]))
        self.assertEqual(reply, dict(folders=[dict(id=None, name='Top'), dict(id=None, name='Other')], files=1))
        self.assertEqual(self.page(command, 'folders', dict(project='project-1', path=['Top']))['folders'],
                         [dict(id=None, name='Sub')])

    def test_failing_folder_request_is_an_error_not_no_folders(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        top = self.app.data.dataHubs.item(0).dataProjects.item(0).rootFolder.dataFolders.item(0)
        top.dataFolders = None  # listing fails
        reply = self.page(command, 'folders', dict(project='project-1', path=['Top']))
        self.assertIn('error', reply)
        self.assertNotIn('folders', reply)
        reply = self.page(command, 'folders', dict(project='project-1', path=['Gone']))
        self.assertIn('no longer there', reply['error'])

    def test_same_named_folders_are_addressed_by_id(self):
        first = fake.folder('Twin', folders=[fake.folder('Inside one')], identity='folder-a')
        second = fake.folder('Twin', folders=[fake.folder('Inside two')], identity='folder-b')
        self.app.data = fake.data([], [first, second])
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        reply = self.page(command, 'folders', dict(project='project-1', path=[]))
        self.assertEqual(reply['folders'], [dict(id='folder-a', name='Twin'), dict(id='folder-b', name='Twin')])
        reply = self.page(command, 'folders', dict(project='project-1', path=['Twin'], ids=['folder-b']))
        self.assertEqual(reply['folders'], [dict(id=None, name='Inside two')])

    def test_ticked_twin_exports_only_that_folder(self):
        one, two = fake.File('One', 'one'), fake.File('Two', 'two')
        self.app.data = fake.data([], [fake.folder('Twin', [one], identity='folder-a'),
                                       fake.folder('Twin', [two], identity='folder-b')])
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-1': [[{'id': 'folder-b', 'name': 'Twin'}]]})
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['two'])
        self.assertEqual(one.calls, 0)

    def test_projects_already_exported_are_marked(self):
        self.picker_data()
        self.open_dialog().execute.handlers[0].notify(None)  # everything into the folder
        command = self.open_dialog()
        self.choose(command)
        hubs = self.page(command, 'hubs')['hubs']
        self.assertEqual([p['exported'] for p in hubs[0]['projects']], [4, 1])
        # Browsing to an empty folder tells the page the marks are gone.
        empty = Path(self.temp.name) / 'empty'
        empty.mkdir()
        self.folder.folder = str(empty)
        self.change(command, command.commandInputs.find('fbx_browse'))
        self.assertEqual(command.commandInputs.find('fbx_tree').sent, [('exported', '{}')])

    def test_ticked_folder_exports_its_subtree(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.page(command, 'hubs')
        self.assertFalse(self.valid(command))
        self.assertEqual(inputs.find('fbx_summary').text, 'Nothing chosen yet.')
        self.page(command, 'selection', {'project-1': [['Top']]})
        self.assertEqual(inputs.find('fbx_summary').text, '0 whole projects and 1 folder chosen.')
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'top'])

    def test_ticked_project_exports_whole_project(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['second'])

    def test_unticking_everything_disables_export(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        self.page(command, 'selection', {})
        self.assertFalse(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertIn('Tick at least one', self.errors[0][0])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_lifecycle_summary_and_report(self):
        self.module.run(None)
        self.assertEqual(self.errors, [])
        command = self.open_dialog()
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.errors, [])
        self.assertIsNone(self.module._busy['by'])  # free for command-line jobs again
        # The results dialog is not started inside the export's execute handler: Fusion starts
        # it from the custom event once the export command has finished.
        results = self.definitions.itemById('FusionBulkExportResults')
        from fusion_bulk_export.results import TITLE
        self.assertEqual(results.name, TITLE)
        self.assertEqual(results.commands, [])
        self.assertEqual(len(self.custom.queue), 1)
        self.end(command)
        self.assertEqual(results.commands, [])  # the event starts it, not the destroy handler
        dialog, data, text = self.results()
        self.assertEqual(self.plain, [])
        self.assertEqual(data['headline'], 'Finished')
        self.assertEqual(data['rows'], [['Exported now', 1]])
        self.assertEqual(data['output'], self.temp.name)
        self.assertEqual(data['retry'], '')
        # One button, Close: OK hidden, Cancel renamed; the page across the whole dialog.
        self.assertFalse(dialog.isOKButtonVisible)
        self.assertEqual(dialog.cancelButtonText, 'Close')
        page = dialog.commandInputs.find('fbx_results')
        self.assertTrue(page.isFullWidth)
        self.assertEqual(page.name, '')
        # The page is this run's file in the app-data folder (beside settings.json), not in the
        # export folder or the add-in's, with the summary already in it: no message needed.
        shown = self.page_file(dialog)
        self.assertEqual(shown.parent, self.settings.parent)  # the page's URL keeps the given path
        self.assertNotIn(self.temp.name, page.htmlFileURL)
        html = shown.read_text(encoding='utf-8')
        self.assertIn('<h1 class="finished">Finished</h1>', html)
        self.assertIn('<span class="label">Exported now</span><span class="value">1</span>', html)
        self.assertIn('>Open report</button>', html)
        self.assertIn('>Open folder</button>', html)
        self.assertNotIn('src="summary.js"', html)  # the script is in the page itself
        folder = Path(self.temp.name)
        self.assertTrue((folder / 'manifest.json').exists())
        self.assertTrue((folder / 'index.csv').exists())
        self.assertEqual(self.page(dialog, 'ready'), {'ok': True})  # logged only
        self.assertEqual(self.page(dialog, 'open', {'what': 'report'}), {'ok': True})
        self.assertEqual([str(path) for path in self.opened], [str(folder / 'index.html')])
        self.assertEqual(self.page(dialog, 'open', {'what': 'folder'}), {'ok': True})
        self.assertEqual(str(self.opened[1]), self.temp.name)
        # Enter or Escape on the page ends the command; its page is deleted.
        self.assertEqual(self.page(dialog, 'close'), {'ok': True})
        self.assertEqual(dialog.executed, [True])
        self.assertIsNone(self.module._results['view'])
        self.assertFalse(shown.exists())
        self.run_checks()
        self.assertEqual(self.plain, [])
        self.assertEqual(self.fallback_log(), [])
        # A second run: nothing new, all up to date.
        self.export()
        data = self.results()[1]
        self.assertEqual(data['rows'], [['Exported now', 0], ['Already up to date', 1]])

    def test_open_failure_is_said_on_the_page(self):
        self.export()
        dialog = self.results()[0]
        with patch('fusion_bulk_export.report.open_path', side_effect=OSError('no viewer')):
            reply = self.page(dialog, 'open', {'what': 'report'})
        self.assertIn('Could not open it (no viewer)', reply['error'])
        self.assertEqual(reply['path'], str(Path(self.temp.name) / 'index.html'))  # to copy
        self.assertEqual(self.errors, [])

    def test_folder_and_report_lines_name_the_same_folder(self):
        # A remembered folder written another way ("..", a trailing separator) is shown as the
        # export used it: "Folder:" and "Report:" (and Open folder) agree, and neither is resolved.
        self.settings.write_text(json.dumps(dict(output=str(Path(self.temp.name) / 'sub' / '..') + os.sep)))
        self.export()
        data = self.results()[1]
        self.assertEqual(data['output'], self.temp.name)
        self.assertEqual(data['report'], str(Path(self.temp.name) / 'index.html'))

    def assert_fallback(self, reason):
        """The summary went to a plain message box: OK only, with the folder and report paths,
        and only that: no results dialog, and why in results-fallback.log."""
        self.run_checks()  # nothing more shows later
        self.assertEqual(len(self.plain), 1, self.plain)
        text, title, buttons = self.plain[0][:3]
        self.assertEqual(buttons, self.adsk.core.MessageBoxButtonTypes.OKButtonType)
        self.assertEqual(title, 'Fusion Bulk Export')
        self.assertTrue(text.startswith('Finished\n'), text)
        self.assertIn('Exported now  1', text)
        self.assertIn('Folder: ' + self.temp.name, text)
        self.assertIn('Report: ' + str(Path(self.temp.name) / 'index.html'), text)
        self.assertNotIn('Traceback', text)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.details, [])
        self.assertIsNone(self.module._results['view'])  # no dialog
        log = self.fallback_log()
        self.assertEqual(len(log), 1, log)
        self.assertIn(reason, log[0])
        self.assertNotIn(self.temp.name, log[0])
        self.assertNotIn('Part', log[0])

    def assert_dialog_only(self):
        """The results dialog shows, and no message box (now or after the check)."""
        self.run_checks()
        self.assertIsNotNone(self.module._results['view'])
        self.assertEqual(self.plain, [])
        self.assertEqual(self.fallback_log(), [])

    def test_fallback_without_html_inputs(self):
        self.module.run(None)
        self.definitions.itemById('FusionBulkExportResults').make_command = lambda: fake.Command(fake.OldInputs())
        self.export()
        self.custom.pump()
        self.assert_fallback('commandCreated raised RuntimeError: this Fusion has no HTML inputs')
        command = self.definitions.itemById('FusionBulkExportResults').commands[-1]
        self.assertTrue(command.isAutoExecute)  # the empty dialog is not shown

    def test_when_the_event_cannot_be_fired_the_export_commands_end_starts_the_dialog(self):
        for fired in (False, None, 'raise'):
            with self.subTest(fired=fired):
                self.plain.clear()

                def fire(name, info, fired=fired):
                    if fired == 'raise':
                        raise RuntimeError('synthetic')
                    return fired
                self.app.fireCustomEvent = fire
                command = self.open_dialog()
                command.execute.handlers[0].notify(None)
                self.assertIsNone(self.module._results['view'])
                self.end(command)  # the export command ended: its destroy handler starts it
                self.assertIsNotNone(self.module._results['view'])
                self.assertEqual(self.plain, [])
                self.module._results['view'].command.destroy.notify(NS())

    def test_a_late_event_after_the_destroy_start_shows_nothing_more(self):
        # The event says it was not fired, but Fusion delivers it anyway after the export ended.
        self.module.run(None)
        self.app.fireCustomEvent = lambda name, info: self.custom.fire(name, info) and False
        self.export()
        definition = self.definitions.itemById('FusionBulkExportResults')
        self.assertEqual(len(definition.commands), 1)
        self.custom.pump()
        self.assertEqual(definition.calls, 1)
        self.assert_dialog_only()

    def test_fallback_when_execute_raises(self):
        self.module.run(None)
        self.definitions.itemById('FusionBulkExportResults').execute_mode = 'raise'
        self.export()
        self.custom.pump()
        self.assert_fallback('definition.execute() raised RuntimeError')

    def test_fallback_when_execute_returns_false(self):
        self.module.run(None)
        self.definitions.itemById('FusionBulkExportResults').execute_mode = 'false'
        self.export()
        self.custom.pump()
        self.assert_fallback('definition.execute() returned False')

    def test_command_created_later_still_shows_the_dialog(self):
        self.module.run(None)
        definition = self.definitions.itemById('FusionBulkExportResults')
        definition.execute_mode, definition.later = 'later', self.custom
        self.export()
        self.custom.pump()  # the event's execute() returns at once; commandCreated follows
        self.assertEqual(len(definition.commands), 1)
        self.assert_dialog_only()

    def test_fallback_when_command_created_never_comes(self):
        self.module.run(None)
        definition = self.definitions.itemById('FusionBulkExportResults')
        definition.execute_mode = 'never'
        self.export()
        self.custom.pump()
        self.assertEqual(self.plain, [])  # not yet: Fusion may be slow
        self.assert_fallback('commandCreated did not run within 120 s after execute() (24 checks)')
        self.assertIn('Nothing is lost', self.plain[0][0])
        self.assertEqual(len(self.plain), 1)  # exactly once
        # Fusion builds it after all, too late: it ends without a dialog (the box showed).
        definition.create()
        self.assertTrue(definition.commands[-1].isAutoExecute)
        self.assertEqual(len(self.plain), 1)

    def test_a_dialog_twenty_seconds_late_after_a_long_run_is_shown_and_no_box(self):
        self.module.run(None)
        definition = self.definitions.itemById('FusionBulkExportResults')
        definition.execute_mode = 'never'  # Fusion is busy: commandCreated comes later
        self.export()
        self.custom.pump()
        for _ in range(4):  # 20 s of checks go by
            timer = self.timers.pop(0)
            self.now[0] += timer.seconds
            timer.function()
            self.custom.pump()
        self.assertEqual(self.plain, [])
        definition.create()  # Fusion builds the dialog at last
        self.assertFalse(definition.commands[-1].isAutoExecute)
        self.assert_dialog_only()

    def test_a_busy_fusion_that_handles_few_checks_is_not_given_up_on(self):
        # Ten minutes pass but Fusion handled only a few checks: still waiting, no box.
        self.module.run(None)
        definition = self.definitions.itemById('FusionBulkExportResults')
        definition.execute_mode = 'never'
        self.export()
        self.custom.pump()
        for _ in range(5):
            timer = self.timers.pop(0)
            self.now[0] += 120
            timer.function()
            self.custom.pump()
        self.assertEqual(self.plain, [])
        self.assertEqual(len(self.timers), 1)  # the next check is on its way
        definition.create()
        self.assert_dialog_only()

    def test_neither_the_event_nor_the_destroy_handler_start_it_then_the_check_does(self):
        self.module.run(None)
        self.app.fireCustomEvent = lambda name, info: self.custom.fire(name, info) if info.startswith('check') else True
        command = self.open_dialog()
        command.execute.handlers[0].notify(None)  # and Fusion never ends the export command
        self.custom.pump()
        self.assertIsNone(self.module._results['view'])
        self.assert_dialog_only()

    def test_fallback_when_the_page_cannot_be_written(self):
        self.module.run(None)
        with patch('fusion_bulk_export.results.write_page', side_effect=PermissionError(13, 'Permission denied')):
            self.export()
        self.assert_fallback('results page not written: PermissionError: [Errno 13] Permission denied')
        self.assertEqual(self.definitions.itemById('FusionBulkExportResults').commands, [])

    def test_fallback_without_the_results_command(self):
        self.definitions.addButtonDefinition('FusionBulkExportResults', 'left over')  # a stale one is replaced
        self.module.run(None)
        self.assertIsNotNone(self.module._results['definition'])
        self.module.remove_results()
        self.export()
        self.assert_fallback('results command or event not registered')

    def test_a_page_that_never_says_anything_still_shows_the_results_and_closes_quietly(self):
        # Fusion drops the page's messages: the summary is in the page itself, and closing the
        # dialog shows no message box.
        self.export()
        command = self.results()[0]
        self.assertIn('Exported now', self.page_file(command).read_text(encoding='utf-8'))
        self.end(command)
        self.run_checks()
        self.assertEqual(self.plain, [])
        self.assertEqual(self.fallback_log(), [])
        self.assertEqual(list(self.settings.parent.glob('results-page-*')), [])

    def test_pages_left_by_an_earlier_session_are_deleted_at_start(self):
        left = self.settings.parent / 'results-page-1-1.html'
        left.write_text('old')
        self.module.run(None)
        self.assertFalse(left.exists())
        self.assertTrue(self.settings.exists())

    def test_older_fusion_names_the_ok_button_close(self):
        self.module.run(None)
        self.definitions.itemById('FusionBulkExportResults').make_command = fake.OldCommand
        self.export()
        command = self.results()[0]
        self.assertTrue(command.isOKButtonVisible)
        self.assertEqual(command.okButtonText, 'Close')
        self.assertEqual(self.plain, [])

    def test_results_command_started_again_shows_the_last_results(self):
        self.module.run(None)
        results = self.definitions.itemById('FusionBulkExportResults')
        results.execute()
        self.assertTrue(results.commands[-1].isAutoExecute)  # nothing to show yet
        self.export()
        command = self.results()[0]
        self.end(command)
        results.execute()
        self.assertIn('Finished', self.page_file(results.commands[-1]).read_text(encoding='utf-8'))
        self.assertEqual(self.plain, [])

    def test_the_handlers_and_state_outlive_a_package_reload(self):
        # The export dialog reloads the package (forget_package); the results' state and
        # handlers live in the entry file, so the run started before still shows.
        self.module.run(None)
        command = self.open_dialog()
        command.execute.handlers[0].notify(None)
        import sys
        saved = {name: module for name, module in sys.modules.items() if name.startswith('fusion_bulk_export')}
        for name in saved:
            del sys.modules[name]
        self.addCleanup(sys.modules.update, saved)
        self.end(command)
        self.custom.pump()
        self.assertIsNotNone(self.module._results['view'])
        self.assertIn(self.module.ResultsCreated, [type(h) for h in self.module._handlers])
        self.module.stop(None)
        self.assertEqual(self.module._handlers, [])
        self.assertIsNone(self.module._results['definition'])
        self.assertNotIn('FusionBulkExportShowResults', self.custom.events)

    def test_names_reach_the_page_escaped(self):
        odd = fake.File('<b>Bad</b> & "x"', 'odd')
        odd.failures = 2
        self.app.data = fake.data([odd])
        self.export()
        command, data, _ = self.results()
        self.assertEqual(data['failures'][0]['name'], '<b>Bad</b> & "x"')
        self.assertNotIn('Bad', command.commandInputs.find('fbx_results').htmlFileURL)
        html = self.page_file(command).read_text(encoding='utf-8')
        self.assertNotIn('<b>Bad</b>', html)
        self.assertIn('&lt;b&gt;Bad&lt;/b&gt; &amp; &quot;x&quot;:', html)

    def test_failure_reasons_and_retry_hint(self):
        self.file.failures = 2  # both attempts fail
        self.export()
        _, data, text = self.results()
        self.assertEqual(data['rows'], [['Exported now', 0], ['Failed', 1]])
        self.assertEqual(data['failures'], [{'name': 'Part', 'reason': 'Synthetic export failure'}])
        self.assertEqual(data['more'], 0)
        self.assertEqual(data['retry'], 'Run again to retry the 1 failed.')
        self.assertIn('- Part: Synthetic export failure', text)
        page = (Path(self.temp.name) / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Synthetic export failure', page)
        self.assertEqual(self.opened, [])

    def test_empty_step_is_a_warning_in_the_summary(self):
        self.file.step_solids = 0
        self.export()
        data = self.results()[1]
        self.assertEqual(data['rows'], [['Exported now', 1], ['Warnings', 1]])

    def test_trouble_asks_try_again_or_stop(self):
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files:
            file.failures = 2
        self.app.data = fake.data(files)
        self.export()
        self.assertEqual(len(self.troubles), 1)
        question = self.troubles[0][0]
        self.assertIn('in a row failed', question)
        self.assertIn('Last error: Synthetic export failure', question)
        self.assertTrue(question.endswith('Try again now?'), question)
        data = self.results()[1]
        self.assertTrue(data['headline'].startswith('Stopped: 5 designs in a row failed'), data['headline'])
        self.assertIn('signed in', ' '.join(data['notes']))
        self.assertEqual(data['more'], 2)  # five failed: three named, two more in the report
        self.assertEqual(sum(file.calls for file in files), 10)

    def test_trouble_retry_carries_on(self):
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files[:5]:
            file.failures = 2
        self.app.data = fake.data(files)
        self.trouble_answer = self.adsk.core.DialogResults.DialogOK
        self.export()
        self.assertEqual(len(self.troubles), 1)
        _, data, text = self.results()
        self.assertEqual(data['rows'][0], ['Exported now', 2])
        self.assertEqual(data['retry'], 'Run again to retry the 5 failed.')
        self.assertIn('and 2 more in the report.', text)

    def test_dialogs_work_without_optional_enum_members(self):
        # Icons are optional; the pause asks OK / Cancel, since there is no DialogRetry result.
        self.assertFalse(hasattr(self.adsk.core.DialogResults, 'DialogRetry'))
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files[:5]:
            file.failures = 2
        self.app.data = fake.data(files)
        self.trouble_answer = self.adsk.core.DialogResults.DialogOK
        with patch.object(self.adsk.core, 'MessageBoxIconTypes', NS()):
            self.export()
        self.assertEqual([len(args) for args in self.troubles], [3])
        self.assertEqual(self.troubles[0][2], self.adsk.core.MessageBoxButtonTypes.OKCancelButtonType)
        self.assertEqual(self.results()[1]['rows'][0], ['Exported now', 2])

    def test_low_space_is_reported(self):
        with patch('fusion_bulk_export.core.free_bytes', return_value=200 << 20):
            self.export()
        self.assertIn('Low disk space: 200 MB free.', self.results()[1]['notes'])

    def test_browse_cancel_keeps_folder_and_tree(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        self.folder.showDialog = lambda: self.adsk.core.DialogResults.DialogCancel
        self.change(command, inputs.find('fbx_browse'))
        self.assertFolderShown(inputs, self.temp.name)
        self.assertTrue(self.valid(command))
        self.assertEqual(self.file.calls, 0)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_browse_sets_folder_and_says_it_continues(self):
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        (other / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=[], entries=[
            dict(key=json.dumps(['hub-1', 'project-1', 'x', 1]), status='done'),
            dict(key=json.dumps(['hub-1', 'project-1', 'y', 1]), status='failed')])))
        (other / 'manifest.journal').write_text(json.dumps(dict(entry=dict(
            key=json.dumps(['hub-1', 'project-2', 'z', 1]), status='done'))) + '\n')
        command = self.open_dialog()
        inputs = command.commandInputs
        self.assertEqual(inputs.find('fbx_note').text, 'A new export into this folder.')
        self.folder.folder = other.as_posix()
        self.change(command, inputs.find('fbx_browse'))
        self.assertFolderShown(inputs, other)
        self.assertEqual(inputs.find('fbx_note').text, 'Continues the export in this folder (2 done).')

    def test_folder_and_mode_are_remembered(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        command.execute.handlers[0].notify(None)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')),
                         dict(output=self.temp.name, mode='Choose projects or folders', other_files=True, step=True))
        inputs = self.open_dialog().commandInputs
        self.assertEqual(inputs.find('fbx_mode').selectedItem.name, 'Choose projects or folders')
        self.assertTrue(inputs.find('fbx_tree').isVisible)
        self.assertFolderShown(inputs, self.temp.name)

    def test_other_files_checkbox_is_on_by_default_and_remembered(self):
        spec = fake.File('Spec', 'spec', extension='pdf')
        self.app.data = fake.data([self.file, spec])
        command = self.open_dialog()
        box = command.commandInputs.find('fbx_files')
        self.assertEqual(box.name, 'Also save other files (PDFs, uploads)')
        self.assertTrue(box.value)
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.results()[1]['rows'], [['Exported now', 1], ['Other files saved', 1]])
        self.assertTrue((Path(self.temp.name) / 'Hub' / 'Project' / 'Spec.pdf').is_file())
        command = self.open_dialog()
        command.commandInputs.find('fbx_files').value = False
        spec.versionNumber = 4
        command.execute.handlers[0].notify(None)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8'))['other_files'], False)
        self.assertEqual(len(spec.downloads), 1)  # not saved with the box off
        self.assertEqual(self.results()[1]['rows'], [['Exported now', 0], ['Already up to date', 1], ['Skipped', 1]])
        self.assertFalse(self.open_dialog().commandInputs.find('fbx_files').value)

    def test_step_checkbox_is_on_by_default_with_a_note_when_off_and_remembered(self):
        from fusion_bulk_export import picker
        part = fake.File('Part', 'part')
        self.app.data = fake.data([part])
        command = self.open_dialog()
        inputs = command.commandInputs
        box, note = inputs.find('fbx_step'), inputs.find('fbx_step_note')
        self.assertEqual(box.name, 'Also save a STEP file for each design')
        self.assertLessEqual(len(box.name), 40)
        self.assertTrue(box.value)
        self.assertIn('opens only in Fusion', box.tooltip)
        self.assertEqual((note.text, note.isVisible, note.isFullWidth), (picker.STEP_OFF_NOTE, False, True))
        self.assertEqual(note.text, 'Without STEP you get only .f3d files, which open only in Fusion.')
        box.value = False
        self.change(command, box)
        self.assertTrue(note.isVisible)
        command.execute.handlers[0].notify(None)
        self.assertIs(json.loads(self.settings.read_text(encoding='utf-8'))['step'], False)
        self.assertEqual(sorted(p.suffix for p in Path(self.temp.name).rglob('Part v3.*')), ['.f3d', '.png'])
        self.assertIn('STEP files were not requested for this export.', self.results()[1]['notes'])
        command = self.open_dialog()
        box = command.commandInputs.find('fbx_step')
        self.assertFalse(box.value)
        self.assertTrue(command.commandInputs.find('fbx_step_note').isVisible)
        box.value = True
        self.change(command, box)
        self.assertFalse(command.commandInputs.find('fbx_step_note').isVisible)
        command.execute.handlers[0].notify(None)
        self.assertTrue(list(Path(self.temp.name).rglob('Part v3.step')))

    def test_an_entry_file_that_does_not_pass_step_uses_the_remembered_choice(self):
        from fusion_bulk_export import command as package
        part = fake.File('Part', 'part')
        self.app.data = fake.data([part])
        self.open_dialog()
        picker = self.module._picker
        self.settings.write_text(json.dumps(dict(output=self.temp.name, step=False)))
        package.export(self.app, picker.selection(), picker.output, show=lambda data: None, other_files=True)
        self.assertFalse(list(Path(self.temp.name).rglob('*.step')))

    def test_no_linked_parts_checkbox_and_no_link_removed_from_the_dialog(self):
        # The copies with linked parts are a testing-only experiment (LINKED_COPIES_ENV): the dialog
        # offers no checkbox, a remembered choice from a test build is dropped, and an export from
        # the dialog removes no link.
        assembly = fake.linked_assembly(fake.File('Assembly', 'assembly', references=1), parts=1)
        self.app.data = fake.data([assembly])
        self.settings.write_text(json.dumps(dict(output=self.temp.name, linked_copies=True)))
        fake.BREAK_LINK_CALLS[0] = 0
        command = self.open_dialog()
        self.assertIsNone(command.commandInputs.find('fbx_linked'))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FUSION_BULK_EXPORT_EXPERIMENT_LINKED_COPIES', None)
            command.execute.handlers[0].notify(None)
        self.assertNotIn('linked_copies', json.loads(self.settings.read_text(encoding='utf-8')))
        self.assertEqual(fake.BREAK_LINK_CALLS[0], 0)
        self.assertFalse(assembly.documents[0].isModified)
        self.assertFalse(any('linked parts' in note for note in self.results()[1]['notes']))

    def test_an_entry_file_that_still_passes_linked_copies_is_accepted_and_ignored(self):
        from fusion_bulk_export import command as package
        assembly = fake.linked_assembly(fake.File('Assembly', 'assembly', references=1), parts=1)
        self.app.data = fake.data([assembly])
        self.open_dialog()
        picker = self.module._picker
        package.export(self.app, picker.selection(), picker.output, show=lambda data: None, other_files=True,
                       linked_copies=True)
        self.assertFalse(assembly.documents[0].isModified)

    def test_no_drawings_checkbox_and_drawings_are_not_supported(self):
        drawing = fake.File('Sheet', 'sheet', extension='f2d')
        self.app.data = fake.data([self.file, drawing])
        self.settings.write_text(json.dumps(dict(drawings=True)))  # remembered by an earlier version
        command = self.open_dialog()
        self.assertIsNone(command.commandInputs.find('fbx_drawings'))
        self.assertIsNone(command.commandInputs.find('fbx_drawings_note'))
        self.assertFalse(any('drawing' in (getattr(c, 'name', '') or '').lower() for c in command.commandInputs.added))
        command.execute.handlers[0].notify(None)
        self.assertEqual(drawing.calls, 0)  # never opened
        data = self.results()[1]
        self.assertIn("1 file can't be exported by an add-in (drawings, electronics). It is not a failure; the report lists it.",
                      data['notes'])
        self.assertNotIn('Failed', [label for label, _ in data['rows']])
        self.assertNotIn('drawings', json.loads(self.settings.read_text(encoding='utf-8')))  # the old setting is dropped

    def test_an_entry_file_loaded_before_the_update_still_exports(self):
        # Fusion keeps the entry file it loaded until the add-in restarts; it still passes drawings=.
        from fusion_bulk_export import command as package
        command = self.open_dialog()
        picker = self.module._picker
        self.assertIs(picker.drawings, False)
        counts = package.export(self.app, picker.selection(), picker.output, show=lambda data: None,
                                other_files=True, drawings=True)
        self.assertEqual(counts['exported'], 1)

    def test_first_use_suggests_documents_folder(self):
        self.settings.unlink()
        with patch('fusion_bulk_export.settings.Path.home', return_value=Path(self.temp.name) / 'home'):
            command = self.open_dialog()
        inputs = command.commandInputs
        suggested = Path(self.temp.name) / 'home' / 'Documents' / 'FusionExport'
        self.assertFolderShown(inputs, suggested)
        self.assertIn('created when the export starts', inputs.find('fbx_note').text)
        self.assertFalse(suggested.exists())
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertTrue((suggested / 'manifest.json').is_file())

    def test_no_folder_disables_export(self):
        command = self.open_dialog()
        self.module._picker.output = ''
        self.assertFalse(self.valid(command))

    def test_progress_cancel_keeps_manifest(self):
        self.progress.wasCancelled = True
        self.open_dialog().execute.handlers[0].notify(None)
        self.assertEqual(self.file.calls, 0)
        data = self.results()[1]
        self.assertEqual(data['headline'], 'Cancelled')
        self.assertIn('Run again to carry on where it stopped.', data['notes'])
        self.assertTrue((Path(self.temp.name) / 'manifest.json').exists())


class FriendlyErrorTests(UIBase):
    def setUp(self):
        super().setUp()
        log = tempfile.TemporaryDirectory(prefix='log-')
        self.addCleanup(log.cleanup)
        self.log = Path(log.name)
        patcher = patch.object(self.module, '_LOG_DIR', str(self.log))
        patcher.start()
        self.addCleanup(patcher.stop)

    def export(self):
        command = self.open_dialog()
        self.errors.clear()
        command.execute.handlers[0].notify(None)
        self.assertEqual(len(self.errors), 1, self.errors)
        text = self.errors[0][0]
        self.assertNotIn('Traceback', text)
        self.assertFalse((self.log / 'errors.log').exists())
        return text

    def test_unusable_folder_is_one_plain_sentence(self):
        target = Path(self.temp.name) / 'a file'
        target.write_text('')
        self.settings.write_text(json.dumps(dict(output=str(target))))
        text = self.export()
        self.assertTrue(text.startswith('This folder cannot be used for the export: '), text)
        self.assertIn('Choose a folder', text)
        self.assertEqual(self.custom.queue, [])
        self.assertEqual(self.plain, [])

    def test_damaged_manifest(self):
        (Path(self.temp.name) / 'manifest.json').write_text('{not json')
        text = self.export()
        self.assertIn('manifest.json in', text)
        self.assertIn('cannot be read', text)
        self.assertEqual(self.file.calls, 0)

    def test_newer_manifest(self):
        (Path(self.temp.name) / 'manifest.json').write_text('{"schema": 99, "entries": [], "runs": []}')
        self.assertIn('newer version', self.export())

    def test_signed_out(self):
        self.app.data.dataHubs.items.clear()
        text = self.export()
        self.assertIn('no hubs', text)
        self.assertIn('Sign in', text)

    def test_unexpected_error_is_logged_with_copyable_details(self):
        command = self.open_dialog()
        self.module._code['export'] = lambda *args, **kwargs: {}['boom']
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.errors, [])
        prompt, title, details = self.details[-1]
        log = self.log / 'errors.log'
        self.assertIn(str(log), prompt)
        self.assertIn('nothing is sent anywhere', prompt)
        self.assertLessEqual(prompt.count('. '), 2)
        self.assertIn('KeyError: <...>', details)  # a key can be a name: left out
        self.assertIn('at FusionBulkExport.py:', details)
        self.assertNotIn('\n', details)
        self.assertIn('KeyError: <...>', log.read_text(encoding='utf-8'))

    def test_unexpected_error_details_hold_no_names(self):
        import errno
        command = self.open_dialog()
        secret = '/Users/someone/FusionExport/Secret Hub/Secret Project/Secret Design v3.step'

        def failing(*args, **kwargs):
            try:
                raise OSError(errno.EACCES, 'Permission denied', secret)
            except OSError as error:
                raise RuntimeError('could not move %s to "Secret Design"' % secret) from error
        self.module._code['export'] = failing
        command.execute.handlers[0].notify(None)
        prompt, title, details = self.details[-1]
        logged = (self.log / 'errors.log').read_text(encoding='utf-8')
        for text in (details, logged):
            self.assertNotIn('Secret', text)
            self.assertNotIn('someone', text)
            self.assertIn('RuntimeError: could not move <path>', text)
            self.assertIn('PermissionError: [Errno 13] Permission denied', text)
            self.assertNotIn('test_ui.py', text)  # only the add-in's own code is listed

    def test_dialog_layout(self):
        """What real Fusion drew squeezed: the tree must span the dialog, labels must not repeat a
        button's caption, checkbox labels must be short enough for the label column, and the
        dialog must open big enough for the tree."""
        command = self.open_dialog()
        inputs = command.commandInputs
        tree = inputs.find('fbx_tree')
        self.assertEqual(tree.name, '')
        self.assertTrue(tree.isFullWidth)
        self.assertGreaterEqual(tree.minimumHeight, 240)
        self.assertEqual(tree.maximumHeight, 0)  # grows with the dialog
        captions = {control.text for control in inputs.added if getattr(control, 'isCheckBox', True) is False}
        self.assertEqual(captions, {'Browse…'})
        for control in inputs.added:
            self.assertNotIn(control.name, captions, control.id)
            if getattr(control, 'isCheckBox', False):
                self.assertLessEqual(len(control.name), 40, control.name)
            if hasattr(control, 'numRows') and not control.name:
                self.assertTrue(control.isFullWidth, control.id)
        self.assertEqual(inputs.find('fbx_browse').name, 'Save to')
        width, height = command.sizes['initial']
        self.assertGreaterEqual(width, 500)
        self.assertGreaterEqual(height, 560)
        self.assertLessEqual(command.sizes['minimum'][0], width)
        self.assertGreaterEqual(command.sizes['minimum'][0], 360)
        # The tree comes last: nothing but tree rows can be pushed below the fold.
        order = [control.id for control in inputs.added]
        self.assertEqual(order, ['fbx_version', 'fbx_browse', 'fbx_folder', 'fbx_note', 'fbx_step', 'fbx_step_note',
                                 'fbx_files', 'fbx_mode',
                                 'fbx_tip', 'fbx_summary', 'fbx_tree'])

    def test_long_output_folder_is_shown_with_its_middle_cut(self):
        from fusion_bulk_export.picker import shown_path
        self.assertEqual(shown_path('C:\\FBT'), 'C:\\FBT')
        long = '/Users/someone/' + 'Deep Folder/' * 10 + 'FusionExport'
        shown = shown_path(long)
        self.assertLessEqual(len(shown), 64)
        self.assertIn('…', shown)
        self.assertTrue(shown.startswith('/Users/'))
        self.assertTrue(shown.endswith('/FusionExport'))
        self.settings.write_text(json.dumps(dict(output=long)))
        folder = self.open_dialog().commandInputs.find('fbx_folder')
        self.assertEqual(folder.text, shown)
        self.assertEqual(folder.tooltip, long)

    def test_old_fusion_offers_everything_only(self):
        self.module.run(None)
        command = NS(execute=Event(), inputChanged=Event(), validateInputs=Event(), incomingFromHTML=Event(),
                     destroy=Event(), commandInputs=fake.OldInputs(),
                     setDialogInitialSize=lambda w, h: None, setDialogMinimumSize=lambda w, h: None)
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        inputs = command.commandInputs
        self.assertEqual([item.name for item in inputs.find('fbx_mode').listItems], ['Everything'])
        self.assertIn('needs a newer Fusion', inputs.find('fbx_old').text)
        self.assertIsNone(inputs.find('fbx_tree'))
        self.change(command, inputs.find('fbx_mode'))
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['file-1'])


class ErrorMappingTests(unittest.TestCase):
    def test_trouble_codes_and_known_failures(self):
        import errno
        from fusion_bulk_export import errors
        from fusion_bulk_export.core import Trouble
        for code, words in (('disk_full', 'disk space'), ('output_missing', 'Reconnect'),
                            ('output_unwritable', 'write to'), ('failing', 'signed in')):
            known = errors.friendly(Trouble(code, 'Reason.'))
            self.assertEqual(known.message, 'The export stopped: Reason.')
            self.assertIn(words, known.next_step)
        self.assertIn('disk space', errors.friendly(OSError(errno.ENOSPC, 'No space left on device')).next_step)
        self.assertIn('Sign in', errors.friendly(TimeoutError('No hubs appeared; is Fusion signed in?')).text())
        self.assertIsNone(errors.friendly(KeyError('x')))
        self.assertIsNone(errors.output_folder(ValueError('something else'), '/x'))
        self.assertIn('cannot be used', errors.output_folder(PermissionError(13, 'Permission denied'), '/x').text())


class ProgressTextTests(unittest.TestCase):
    def state(self, **changes):
        state = dict(phase='listing', project=3, projects=17, project_name='P', path=['A', 'B'], design=40,
                     name=None, folder_files=None, folder_item=0, elapsed=725, seconds_per_design=None,
                     counts=dict(exported=0, up_to_date=0, files=0, skipped=0, failed=2))
        state.update(changes)
        return state

    def test_listing_and_design_lines(self):
        from fusion_bulk_export.command import describe
        self.assertEqual(describe(self.state()),
                         'Project 3 of 17, listing: P / A / B\n40 items so far\n12 min · 2 failed')
        state = self.state(phase='design', design=41, name='Gear 50%', seconds_per_design=3.42, elapsed=4000)
        self.assertEqual(describe(state),
                         'Project 3 of 17, design 41: Gear 50%%\nP / A / B\n1 h 06 min · 3.4 s/design · 2 failed')
        state.update(kind='file', name='Spec.pdf')
        self.assertTrue(describe(state).startswith('Project 3 of 17, file 41: Spec.pdf\n'))
        state.update(kind='drawing', name='Sheet')  # from no current code: shown as a design, never "opening"
        self.assertNotIn('drawing', describe(state).lower())

    def test_the_engine_says_which_items_are_other_files(self):
        from fusion_bulk_export.core import Exporter
        from fusion_bulk_export.fusion import FusionAdapter
        states = []
        with tempfile.TemporaryDirectory() as out:
            Exporter(out, FusionAdapter(NS(documents=fake.Documents()))).run(
                fake.data([fake.File('Part', 'part'), fake.File('Spec.pdf', 'spec', extension='pdf')]),
                progress=states.append)
        self.assertEqual([s['kind'] for s in states if s['phase'] == 'design'], ['design', 'file'])
        states = []
        with tempfile.TemporaryDirectory() as out:
            Exporter(out, FusionAdapter(NS(documents=fake.Documents()))).run(
                fake.data([fake.File('Sheet', 'sheet', extension='f2d')]), progress=states.append)
        self.assertEqual([s['kind'] for s in states if s['phase'] == 'design'], ['file'])

    def test_bar_moves_within_a_folder_of_a_single_project(self):
        from fusion_bulk_export.command import bar
        single = dict(projects=1, project=1, phase='design', folder_files=4)
        self.assertEqual([bar(dict(single, folder_item=n))[1] for n in (1, 2, 3, 4)], [0, 250, 500, 750])
        self.assertEqual(bar(dict(single, folder_item=1))[0], 1000)
        # Unknown file count: whole projects only.
        self.assertEqual(bar(dict(projects=3, project=2, phase='design', folder_files=None, folder_item=9)),
                         (3000, 1000))
        self.assertEqual(bar(dict(projects=0, project=0, phase='listing')), (1000, 0))


class ProgressStateTests(unittest.TestCase):
    def test_engine_reports_folder_position_rate_and_failures(self):
        from fusion_bulk_export.core import Exporter
        from fusion_bulk_export.fusion import FusionAdapter
        files = [fake.File('F%d' % n, 'f%d' % n) for n in range(3)]
        files[1].failures = 2
        sub = fake.folder('Sub', files=[fake.File('S', 's')])
        data = fake.data(files, [sub])
        states = []
        ticks = iter(range(0, 10000, 2))
        with tempfile.TemporaryDirectory() as out:
            exporter = Exporter(out, FusionAdapter(NS(documents=fake.Documents())))
            exporter.clock = lambda: next(ticks)
            exporter.run(data, progress=states.append)
            again = []
            Exporter(out, FusionAdapter(NS(documents=fake.Documents()))).run(data, progress=again.append)
        designs = [s for s in states if s['phase'] == 'design']
        self.assertEqual([(s['folder_item'], s['folder_files']) for s in designs], [(1, 3), (2, 3), (3, 3), (1, 1)])
        self.assertEqual(designs[2]['counts']['failed'], 1)
        self.assertEqual(designs[0]['seconds_per_design'], None)
        self.assertGreater(designs[1]['seconds_per_design'], 0)
        self.assertGreater(designs[-1]['elapsed'], designs[0]['elapsed'])
        listing = [s for s in states if s['phase'] == 'listing']
        self.assertEqual([s['folder_files'] for s in listing], [3, 1])
        # Up-to-date designs are not timed.
        self.assertIsNone([s for s in again if s['phase'] == 'design'][1]['seconds_per_design'])


class TreeLayoutContractTests(UIBase):
    """The tree's height is set by construction, never left to the page's content: on macOS a
    test build showed the tree as a ~40-unit strip in "Choose projects or folders" mode."""
    def test_tree_height_is_explicit_when_made_and_each_time_it_is_shown(self):
        from fusion_bulk_export import picker
        command = self.open_dialog()
        tree = command.commandInputs.find('fbx_tree')
        self.assertGreaterEqual(picker.TREE_MIN_HEIGHT, 240)
        self.assertEqual((tree.minimumHeight, tree.maximumHeight), (picker.TREE_MIN_HEIGHT, picker.TREE_MAX_HEIGHT))
        self.assertTrue(tree.isFullWidth)
        self.assertFalse(tree.isVisible)
        tree.minimumHeight = 40  # whatever Fusion did to it while hidden
        self.choose(command)
        self.assertTrue(tree.isVisible)
        self.assertEqual(tree.minimumHeight, picker.TREE_MIN_HEIGHT)
        # Remembered Choose mode: the dialog opens with the tree shown and sized.
        self.settings.write_text(json.dumps(dict(output=self.temp.name, mode='Choose projects or folders')))
        tree = self.open_dialog().commandInputs.find('fbx_tree')
        self.assertTrue(tree.isVisible)
        self.assertEqual(tree.minimumHeight, picker.TREE_MIN_HEIGHT)

    def test_no_maximum_is_given_only_at_creation(self):
        # Real Fusion (Windows retest): setting maximumHeight = 0 raised "3 : invalid argument
        # value" twice per dialog; addBrowserCommandInput(..., 0) itself is accepted.
        from fusion_bulk_export import picker
        set_to = []

        class Tree:
            def __setattr__(self, name, value):
                set_to.append((name, value))
                if name == 'maximumHeight' and value == 0:
                    raise RuntimeError('3 : invalid argument value')
                object.__setattr__(self, name, value)
        traced = []
        picker.size_tree(Tree(), traced.append)
        self.assertEqual((set_to, traced), ([('minimumHeight', picker.TREE_MIN_HEIGHT)], []))
        with patch.object(picker, 'TREE_MAX_HEIGHT', 900):
            set_to[:] = []
            picker.size_tree(Tree(), traced.append)
            self.assertEqual(set_to, [('minimumHeight', picker.TREE_MIN_HEIGHT), ('maximumHeight', 900)])
        inputs = self.open_dialog().commandInputs
        tree = inputs.find('fbx_tree')
        self.assertEqual((tree.minimumHeight, tree.maximumHeight), (picker.TREE_MIN_HEIGHT, 0))  # 0 from creation

    def test_an_older_fusion_without_the_height_properties_still_opens(self):
        inputs = fake.Inputs()
        made = inputs.addBrowserCommandInput

        def browser(*args):
            control = made(*args)

            class Fixed(type(control)):
                def __setattr__(self, name, value):
                    if name in ('minimumHeight', 'maximumHeight'):
                        raise AttributeError(name)
                    super().__setattr__(name, value)
            control.__class__ = Fixed
            return control
        inputs.addBrowserCommandInput = browser
        command = fake.Command(inputs)
        if self.definition is None:
            self.module.run(None)
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        self.choose(command)
        self.assertTrue(command.commandInputs.find('fbx_tree').isVisible)

    def test_the_results_dialog_shares_no_input_with_the_tree(self):
        command = self.open_dialog()
        ids = [control.id for control in command.commandInputs.added]
        self.assertEqual(len(ids), len(set(ids)))
        from fusion_bulk_export import results
        source = Path(results.__file__).read_text(encoding='utf-8')
        self.assertNotIn('fbx_tree', source)
        self.assertNotIn('tree.html', source)
        self.assertNotIn('fbx_results', ids)

    def test_every_control_is_in_view_at_first_on_a_1080_point_screen(self):
        # In Choose mode the checkbox once sat below the tree, out of view: now the tree is last,
        # so only tree rows (which scroll inside the tree) can be below the dialog's first size.
        from fusion_bulk_export import picker
        command = self.open_dialog()
        self.choose(command)
        shown = [control.id for control in command.commandInputs.added if control.isVisible]
        self.assertEqual(shown, ['fbx_version', 'fbx_browse', 'fbx_folder', 'fbx_note', 'fbx_step', 'fbx_files',
                                 'fbx_mode', 'fbx_summary', 'fbx_tree'])
        width, height = picker.DIALOG_SIZE
        self.assertLessEqual(height, 1080 - 200)  # menu bar, dock or taskbar, title bar, margins
        # Rows above the tree at about 30 units each, the OK/Cancel row about 60: the tree's
        # minimum still fits in the first size.
        # Also with the STEP box unticked, when its note shows too.
        self.assertLessEqual(len(shown) * 30 + picker.TREE_MIN_HEIGHT + 60, height)
        self.assertLessEqual(picker.DIALOG_MINIMUM[1], height)


class ResultsSwitchTests(UIBase):
    """results.DIALOG_ENABLED: one switch for the results dialog, on as shipped."""
    results_dialog = False

    def test_on_as_shipped(self):
        from fusion_bulk_export import results
        source = Path(results.__file__).read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'^DIALOG_ENABLED = (.*)$', source, re.M), ['True'])  # the shipped default

    def test_off_the_summary_goes_straight_to_a_message_box(self):
        self.export()
        self.assertIsNone(self.definitions.itemById('FusionBulkExportResults'))  # never registered
        self.assertEqual(self.custom.queue, [])  # nothing fired
        self.assertEqual(len(self.plain), 1)
        self.assertTrue(self.plain[0][0].startswith('Finished'))
        self.assertIn('Exported now', self.plain[0][0])
        self.assertIn('switched off', self.fallback_log()[0])

    def test_on_it_shows_the_results_dialog(self):
        with patch('fusion_bulk_export.results.DIALOG_ENABLED', True):
            self.export()
            self.custom.pump()
        self.assertEqual(self.plain, [])
        self.assertEqual(len(self.definitions.itemById('FusionBulkExportResults').commands), 1)


class ResultsTraceTests(UIBase):
    def test_every_stage_is_traced_with_the_switch_file(self):
        (self.addin / 'trace').write_text('')
        with patch.object(self.module, '_DIR', str(self.addin)):
            self.export()
            command = self.results()[0]
            self.page(command, 'ready')
            self.page(command, 'open', {'what': 'folder'})
            self.end(command)
            self.run_checks()
        log = (self.addin / 'trace.log').read_text(encoding='utf-8')
        for stage in ('results definition created', 'results event registered', 'results page written, run 1',
                      'results event fired: True', 'export command destroyed', 'results event handler: show:1',
                      'results definition found', 'results execute returned: True', 'results commandCreated',
                      'results: browser input added', 'results dialog built', 'results page says: ready',
                      'results page says: open', 'results page answered: ok', 'results dialog closed',
                      'results event handler: check:1', 'results check: closed'):
            self.assertIn(stage.replace(':1', ':N').replace('run 1', 'run N'), re.sub(r'(:|run )\d+', r'\1N', log))
        self.assertNotIn(self.temp.name, log)

    def test_the_fallback_reason_is_traced_and_logged_without_the_switch_file(self):
        self.module.run(None)
        self.definitions.itemById('FusionBulkExportResults').execute_mode = 'false'
        self.export()
        self.custom.pump()
        self.assertFalse((self.addin / 'trace.log').exists())
        self.assertIn('definition.execute() returned False', self.fallback_log()[0])


class ResultsPageTests(unittest.TestCase):
    """The page the add-in writes for each run: the summary is in it as HTML, readable with no
    script; names escaped; the script inlined; nothing loaded from outside."""
    def setUp(self):
        fake.install()
        from fusion_bulk_export import report, results
        self.results, self.report = results, report
        self.data = dict(headline='Stopped: The disk is full.', rows=[['Exported now', 0], ['Already up to date', 0],
                         ['Failed', 5]], failures=[dict(name='D%d <i>' % n, reason='r & s') for n in range(3)],
                         more=2, retry='Run again to retry the 5 failed.', notes=['Low disk space: 200 MB free.'],
                         output='/Users/you/<Export>', report='')

    def text(self, html):
        """What a reader sees with scripts off: the page's text without tags or the script."""
        import html as markup
        body = re.sub(r'<script>.*?</script>', '', html, flags=re.S)
        body = re.sub(r'<style>.*?</style>', '', body, flags=re.S)
        return ' '.join(markup.unescape(re.sub(r'<[^>]+>', ' ', body)).split())

    def test_the_summary_reads_without_scripts(self):
        html = self.results.page_html(self.data)
        text = self.text(html)
        for part in ('Stopped: The disk is full.', 'Exported now 0', 'Failed 5', 'D0 <i>: r & s',
                     'and 2 more in the report', 'Run again to retry the 5 failed.', 'Low disk space: 200 MB free.',
                     'Saved in /Users/you/<Export>', 'Open folder'):
            self.assertIn(part, text)
        self.assertNotIn('Already up to date', text)  # a zero row other than the first
        self.assertNotIn('Open report', text)  # no report was written
        self.assertIn('<h1 class="stopped">', html)
        self.assertIn('class="count failed"', html)
        self.assertNotIn('<i>', html)
        self.assertNotIn('src=', html)
        self.assertNotIn('http', html)
        self.assertIn('function deliver(', html)  # the buttons' script, inlined
        self.assertNotIn('<!--SUMMARY-->', html)

    def test_written_into_the_folder_given_and_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.results.write_page(dict(self.data, report='/r/index.html'), folder)
            self.assertEqual(first.parent, Path(folder))
            self.assertIn('data-path="/r/index.html">Open report</button>', first.read_text(encoding='utf-8'))
            (Path(folder) / 'settings.json').write_text('{}')
            self.results.remove_pages(folder)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['settings.json'])

    def test_a_damaged_template_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / 'summary.html'
            broken.write_text('<html></html>')
            with patch.object(self.results, 'TEMPLATE', broken):
                with self.assertRaises(ValueError):
                    self.results.page_html(self.data)


class HeartbeatTests(UIBase):
    def test_the_heartbeat_says_whether_an_export_runs(self):
        watcher = self.module.Watcher()
        watcher.poll()
        self.assertIsNone(json.loads((self.addin / 'autorun.alive').read_text(encoding='utf-8'))['busy'])
        self.module._busy['by'] = 'button'
        try:
            watcher.poll()
        finally:
            self.module._busy['by'] = None
        beat = json.loads((self.addin / 'autorun.alive').read_text(encoding='utf-8'))
        self.assertEqual(beat['busy'], 'button')
        self.assertGreater(beat['time'], 0)


class OutputRootTests(UIBase):
    def test_a_windows_output_folder_over_200_characters_is_refused_in_the_dialog(self):
        from fusion_bulk_export import names
        long = 'C:\\' + '\\'.join(['Folder %02d' % n + 'x' * 30 for n in range(6)])
        self.settings.write_text(json.dumps(dict(output=long)))
        with patch('fusion_bulk_export.picker.output_root_problem',
                   side_effect=lambda path: names.output_root_problem(path, 'win32')):
            command = self.open_dialog()
            note = command.commandInputs.find('fbx_note').text
            self.assertIn('on Windows it can be at most 200', note)
            self.assertFalse(self.valid(command))  # Export stays greyed out
            self.settings.write_text(json.dumps(dict(output='C:\\FusionExport')))
            self.assertTrue(self.valid(self.open_dialog()))
