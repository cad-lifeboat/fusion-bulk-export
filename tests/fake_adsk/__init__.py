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
        body = NS(isSolid=True)
        component = NS(bRepBodies=Collection(body, NS(isSolid=False)))
        self.products = NS(itemByProductType=lambda product: NS(
            exportManager=Manager(file), allComponents=Collection(component)) if file.design else None)

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
