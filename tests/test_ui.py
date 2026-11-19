"""Synthetic command lifecycle and output-folder dialog checks."""
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake

fake.install()


class Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)


class UItests(unittest.TestCase):
    def setUp(self):
        import adsk
        self.adsk = adsk
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.folder = NS(title='', folder=self.temp.name, showDialog=lambda: 1)
        adsk.core.DialogResults = NS(DialogOK=1)
        self.progress = NS(isCancelButtonShown=False, wasCancelled=False,
                           show=lambda *args: None, hide=lambda: None)
        self.summaries, self.errors = [], []
        self.definition = NS(commandCreated=Event(), deleteMe=lambda: None)
        self.control = NS(deleteMe=lambda: None)
        self.definitions = NS(addButtonDefinition=lambda *args: self.definition)
        self.ui = NS(createFolderDialog=lambda: self.folder,
                     createProgressDialog=lambda: self.progress,
                     inputBox=lambda *args: self.summaries.append(args),
                     messageBox=lambda *args: self.errors.append(args),
                     commandDefinitions=self.definitions,
                     allToolbarPanels=NS(itemById=lambda name: NS(controls=NS(addCommand=lambda definition: self.control))))
        self.file = fake.File()
        self.app = NS(userInterface=self.ui, documents=fake.Documents(), data=fake.data([self.file]))
        adsk.core.Application = NS(get=lambda: self.app)
        adsk.doEvents = lambda: None
        self.module = importlib.import_module('FusionBulkExport')
        self.addCleanup(self.module.stop, None)

    def open_dialog(self):
        self.module.run(None) if not self.definition.commandCreated.handlers else None
        command = NS(execute=Event(), inputChanged=Event(), commandInputs=fake.Inputs(),
                     setDialogInitialSize=lambda w, h: None, setDialogMinimumSize=lambda w, h: None)
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        return command

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
        manifest = json.loads((Path(self.temp.name) / 'manifest.json').read_text())
        return sorted(e['id'] for e in manifest['entries'] if e['status'] == 'done')

    def test_dialog_defaults_to_everything(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        self.assertEqual(inputs.find('fbx_mode').selectedItem.name, 'Everything')
        self.assertFalse(inputs.find('fbx_hub_0').isVisible)
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'other', 'root', 'second', 'top'])

    def test_choose_whole_project(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        self.assertTrue(inputs.find('fbx_hub_0').isVisible)
        inputs.find('fbx_p_0_1_whole').value = True
        self.change(command, inputs.find('fbx_p_0_1_whole'))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['second'])

    def test_pick_folders_lists_them_lazily_and_exports_subtree(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        project = inputs.find('fbx_p_0_0')
        self.assertEqual([c.name for c in project.children.added], ['Whole project', 'Pick folders'])
        inputs.find('fbx_p_0_0_pick').value = True
        self.change(command, inputs.find('fbx_p_0_0_pick'))
        self.assertEqual([c.name for c in project.children.added[2:]], ['Top', '    Sub', 'Other'])
        project.children.added[2].value = True  # Top, which includes Sub
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'top'])

    def test_hidden_folder_choices_do_not_count_and_hub_box_ticks_all(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        pick = inputs.find('fbx_p_0_0_pick')
        pick.value = True
        self.change(command, pick)
        inputs.find('fbx_p_0_0').children.added[4].value = True  # Other
        pick.value = False
        self.change(command, pick)
        every = inputs.find('fbx_hub_0_all')
        every.value = True
        self.change(command, every)
        self.assertTrue(inputs.find('fbx_p_0_0_whole').value and inputs.find('fbx_p_0_1_whole').value)
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'other', 'root', 'second', 'top'])

    def test_choose_with_nothing_ticked_explains_and_exports_nothing(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.summaries, [])
        self.assertIn('Tick at least one', self.errors[0][0])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_lifecycle_and_copyable_folder_dialog(self):
        self.module.run(None)
        self.assertEqual(self.errors, [])
        command = self.open_dialog()
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.summaries), 1)
        message, title, folder = self.summaries[0]
        self.assertIn('Done: 1', message)
        self.assertEqual(title, 'Fusion Bulk Export')
        self.assertEqual(folder, self.temp.name)
        self.assertTrue((Path(folder) / 'manifest.json').exists())

    def test_folder_selection_cancel_does_not_open(self):
        self.folder.showDialog = lambda: 0
        self.module.Execute().notify(None)
        self.assertEqual(self.file.calls, 0)
        self.assertEqual(self.summaries, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_progress_cancel_keeps_manifest(self):
        self.progress.wasCancelled = True
        self.module.Execute().notify(None)
        self.assertEqual(self.file.calls, 0)
        self.assertIn('Cancelled', self.summaries[0][0])
        self.assertTrue((Path(self.temp.name) / 'manifest.json').exists())
