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
        self.childReferences = Collection(*([NS()] if references else []))
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
        return path

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
        self.products = NS(itemByProductType=lambda product: NS(exportManager=Manager(file)) if file.design else None)

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
    adsk = ModuleType('adsk')
    adsk.core = ModuleType('adsk.core')
    adsk.fusion = ModuleType('adsk.fusion')
    adsk.fusion.Design = NS(cast=lambda value: value)
    adsk.fusion.FusionDocument = NS(cast=lambda value: value)
    for name, module in [('adsk', adsk), ('adsk.core', adsk.core), ('adsk.fusion', adsk.fusion)]:
        sys.modules[name] = module
