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
    """A cloud file. references=True (or a number) gives it linked designs, which Fusion opens
    together with it, like the components of an assembly."""
    def __init__(self, name='Part', identity='file-1', version=3, extension='f3d', references=False, design=True):
        self.name, self.id, self.versionNumber = name, identity, version
        self.fileExtension, self.dateModified = extension, 1700000000
        linked = [File('Linked part', 'linked-' + identity + ('' if n == 0 else '-%d' % (n + 1)))
                  for n in range(int(references))]
        self.childReferences = Collection(*linked)
        self.references, self.design = references, design
        self.failures = 0
        self.calls = 0
        self.documents = []


def folder(name='Root', files=(), folders=(), identity=None):
    """A DataFolder; with identity it has an .id like the real one (most tests go without)."""
    result = NS(name=name, dataFiles=Collection(*files), dataFolders=Collection(*folders))
    if identity is not None:
        result.id = identity
    return result


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
    def __init__(self, file, owner=None):
        self.dataFile = file
        self.closed = False
        self.owner = owner  # the Documents collection it is listed in, once opened there
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
        if self.owner is not None and self in self.owner.items:
            self.owner.items.remove(self)
        return True


class Documents(Collection):
    """Application.documents. Opening a design also opens the designs it references (as Fusion
    does for an assembly), unless one of them is open already; all are listed until closed."""
    def open(self, file, visible):
        assert visible is False
        file.calls += 1
        document = Document(file, self)
        file.documents.append(document)
        self.items.append(document)
        for linked in collection_items(file.childReferences):
            if not any(open_.dataFile is not None and open_.dataFile.id == linked.id for open_ in self.items):
                reference = Document(linked, self)
                linked.documents.append(reference)
                self.items.append(reference)
        return document


def collection_items(values):
    return [values.item(index) for index in range(values.count)]


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
    adsk.core.ValidateInputsEventHandler = object
    adsk.core.HTMLEventHandler = object
    adsk.core.DropDownStyles = NS(TextListDropDownStyle=1)
    # Documented enums of messageBox and the dialogs (values here are arbitrary but distinct).
    adsk.core.MessageBoxButtonTypes = NS(OKButtonType=0, OKCancelButtonType=1, RetryCancelButtonType=2,
                                         YesNoButtonType=3, YesNoCancelButtonType=4)
    adsk.core.MessageBoxIconTypes = NS(NoIconIconType=0, QuestionIconType=1, InformationIconType=2,
                                       WarningIconType=3, CriticalIconType=4)
    adsk.core.DialogResults = NS(DialogError=-1, DialogOK=0, DialogCancel=1, DialogNo=2, DialogYes=3,
                                 DialogRetry=4)
    for name, module in [('adsk', adsk), ('adsk.core', adsk.core), ('adsk.fusion', adsk.fusion)]:
        sys.modules[name] = module


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
        return self._add(Group(id, name))

    def addBoolValueInput(self, id, name, is_check_box, resources, value):
        return self._add(NS(id=id, name=name, value=value, isVisible=True, isEnabled=True))

    def addBrowserCommandInput(self, id, name, url, minimum_height, maximum_height=0):
        return self._add(NS(id=id, name=name, htmlFileURL=url, isVisible=True))

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


class Group:
    """Like Fusion while a dialog is being built, a group cannot be folded."""
    def __init__(self, id, name):
        self.id, self.name, self.isVisible, self.children = id, name, True, Inputs()
        self._expanded = True

    @property
    def isExpanded(self):
        return self._expanded

    @isExpanded.setter
    def isExpanded(self, value):
        if not value:
            raise RuntimeError('3 : the group cannot be folded')
        self._expanded = value


class OldInputs(Inputs):
    """Command inputs of an older Fusion, without HTML (browser) inputs in command dialogs."""
    def __getattribute__(self, name):
        if name == 'addBrowserCommandInput':
            raise AttributeError(name)
        return super().__getattribute__(name)
