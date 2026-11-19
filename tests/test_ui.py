"""Synthetic command lifecycle and output-folder dialog checks."""
import importlib
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

    def test_lifecycle_and_copyable_folder_dialog(self):
        self.module.run(None)
        self.assertEqual(self.errors, [])
        command = NS(execute=Event())
        self.definition.commandCreated.handlers[0].notify(NS(command=command))
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
