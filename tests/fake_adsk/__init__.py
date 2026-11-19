"""Synthetic documented-API surface, with no real CAD data."""
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS


class Collection:
    def __init__(self, *items):
        self.items = list(items)

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class File:
    def __init__(self, name='Part', identity='file-1', version=3, extension='f3d', references=False, design=True):
        self.name, self.id, self.versionNumber = name, identity, version
        self.fileExtension, self.dateModified = extension, 1700000000
        self.childReferences = Collection(*([NS(id='linked-' + identity, name='Linked part')] if references else []))
        self.references, self.design = references, design
        self.failures = 0
        self.calls = 0
        self.documents = []


def folder(name='Root', files=(), folders=()):
    return NS(name=name, dataFiles=Collection(*files), dataFolders=Collection(*folders))


def data(files=(), folders=()):
    project = NS(id='project-1', name='Project', rootFolder=folder(files=files, folders=folders))
    hub = NS(id='hub-1', name='Hub', dataProjects=Collection(project))
    return NS(dataHubs=Collection(hub))


class Manager:
    def __init__(self, file):
        self.file = file

    def createFusionArchiveExportOptions(self, path):
        # Like Fusion: the archive is always .f3d, whatever extension is asked for.
        return path if path.endswith('.f3d') else path + '.f3d'

    def createSTEPExportOptions(self, path):
        return path

    def execute(self, path):
        if self.file.failures:
            self.file.failures -= 1
            raise RuntimeError('Synthetic export failure')
        Path(path).write_bytes(b'Synthetic CAD placeholder')
        return True


class Document:
    def __init__(self, file):
        self.dataFile = file
        self.closed = False
        self.documentReferences = Collection(*([NS()] if file.references else []))
        self.isModified = False
        body = NS(isSolid=True, isLightBulbOn=True)
        hidden = NS(isSolid=True, isLightBulbOn=False)
        component = NS(id='root', bRepBodies=Collection(body, NS(isSolid=False, isLightBulbOn=True)),
                       allOccurrences=Collection())
        part = NS(id='part', bRepBodies=Collection(hidden))
        component.allOccurrences = Collection(NS(isVisible=True, component=part))
        self.products = NS(itemByProductType=lambda product: NS(
            exportManager=Manager(file), rootComponent=component,
            allComponents=Collection(component, part)) if file.design else None)

    def close(self, save):
        assert save is False
        self.closed = True
        return True


class Documents(Collection):
    def open(self, file, visible):
        assert visible is False
        file.calls += 1
        document = Document(file)
        file.documents.append(document)
        return document


def install():
    if getattr(sys.modules.get('adsk'), '_synthetic', False):
        return
    adsk = ModuleType('adsk')
    adsk._synthetic = True
    adsk.core = ModuleType('adsk.core')
    adsk.fusion = ModuleType('adsk.fusion')
    adsk.fusion.Design = NS(cast=lambda value: value)
    adsk.fusion.FusionDocument = NS(cast=lambda value: value)
    adsk.core.CustomEventHandler = object
    adsk.core.InputChangedEventHandler = object
    adsk.core.DropDownStyles = NS(TextListDropDownStyle=1)
    for name, module in [('adsk', adsk), ('adsk.core', adsk.core), ('adsk.fusion', adsk.fusion)]:
        sys.modules[name] = module


class UploadFuture:
    def __init__(self, file, states=(0, 1)):
        self.dataFile = file
        self.states = list(states)
        self.polls = 0

    @property
    def uploadState(self):
        self.polls += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class UploadFolder:
    def __init__(self):
        self.name = 'Root'
        self.dataFiles = Collection()
        self.dataFolders = Collection()
        self.futures = []
        self.uploads = []
        self.next_states = (0, 1)
        self.processing_polls = 0

    def uploadFile(self, path):
        self.uploads.append(path)
        file = File(Path(path).stem, 'upload-' + str(len(self.uploads)),
                    references=Path(path).suffix.lower() == '.f3z')
        file.isComplete = False

        def refresh(handler):
            assert handler is None
            self.processing_polls += 1
            file.isComplete = self.processing_polls % 2 == 0
            return True

        file.refresh = refresh
        self.dataFiles.items.append(file)
        future = UploadFuture(file, self.next_states)
        self.futures.append(future)
        return future


class Projects(Collection):
    def __init__(self, *items):
        super().__init__(*items)
        self.created = []

    def add(self, name):
        self.created.append(name)
        project = NS(id='test-project-' + str(len(self.created)), name=name, rootFolder=UploadFolder())
        self.items.append(project)
        return project


class ListItems(list):
    def add(self, name, selected, icon=''):
        item = NS(name=name, isSelected=selected)
        self.append(item)
        return item


class DropDown:
    def __init__(self, id, name):
        self.id, self.name, self.listItems, self.isVisible = id, name, ListItems(), True

    @property
    def selectedItem(self):
        return next((item for item in self.listItems if item.isSelected), None)

    def select(self, name):
        for item in self.listItems:
            item.isSelected = item.name == name


class Inputs:
    """Command inputs: the dialog controls an add-in creates."""
    def __init__(self):
        self.added = []

    def _add(self, control):
        self.added.append(control)
        return control

    def addDropDownCommandInput(self, id, name, style):
        return self._add(DropDown(id, name))

    def addGroupCommandInput(self, id, name):
        return self._add(NS(id=id, name=name, isExpanded=True, isVisible=True, children=Inputs()))

    def addBoolValueInput(self, id, name, is_check_box, resources, value):
        return self._add(NS(id=id, name=name, value=value, isVisible=True, isEnabled=True))

    def addTextBoxCommandInput(self, id, name, text, rows, read_only):
        return self._add(NS(id=id, name=name, text=text, isVisible=True))

    def find(self, id):
        for control in self.added:
            if control.id == id:
                return control
            if hasattr(control, 'children'):
                found = control.children.find(id)
                if found is not None:
                    return found
        return None
