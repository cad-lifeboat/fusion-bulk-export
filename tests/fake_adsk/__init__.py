"""Synthetic documented-API surface, with no real CAD data."""
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import zipfile


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
        # DataFile.hasChildReferences / hasOutofDateChildReferences: plain properties.
        self.hasChildReferences = bool(linked)
        self.hasOutofDateChildReferences = False
        # Set to a message: Documents.open raises it, as when a linked design is missing.
        self.open_error = None
        self.step_solids = 1  # solids in the STEP the synthetic exporter writes
        # DataFile.thumbnail: a DataObjectFuture, finished here (see thumbnail()).
        self.thumbnail = thumbnail()
        self.root = None      # a root component (see component()) to use instead of the default
        self.failures = 0
        self.calls = 0
        self.documents = []
        # DataFile.download(folder, None): how the synthetic Fusion answers ('ok', 'false',
        # 'raise', 'nothing', 'two' files, or 'pdf': the file comes down as a PDF), and what.
        self.download_mode = 'ok'
        # A drawing (.f2d): how its PDF export behaves: 'now', 'later' (written a few doEvents
        # after execute returns, as seen on real Fusion), 'never' (True but no file), 'false',
        # 'partial' (a PDF that stops before its end).
        self.pdf_mode = 'now'
        # Seen on real Fusion: the first execute() on a freshly opened drawing returns True and
        # writes nothing; the next one writes the PDF.
        self.first_pdf_empty = True
        self.products_flaky = 0
        # Seen with old or linked designs: Fusion marks a document modified as it opens it
        # (a recompute, an updated reference), although nobody edited it.
        self.modified_on_open = False
        self.close_result = True  # what Document.close returns (Fusion can answer False)
        self.content = b'Synthetic file content of ' + identity.encode('utf-8')
        self.downloads = []

    def download(self, path, handler):
        """Documented form: download(path, handler); with no handler it is synchronous and
        returns whether it worked. Writes the file under its name into the folder path."""
        assert handler is None
        self.downloads.append(path)
        assert len(path) <= 259 and not path.startswith('\\\\?\\'), 'Fusion would hang on ' + path
        if str(self.fileExtension).lower() in ('f2d', 'fsch', 'fbrd', 'flbr'):
            raise RuntimeError('3 : file type not available for download')  # as real Fusion does
        if self.download_mode == 'raise':
            raise RuntimeError('Synthetic download failure')
        if self.download_mode == 'false':
            return False
        if self.download_mode == 'nothing':
            return True
        # Like Fusion: a folder gets the file under its name (which, for an uploaded file, already
        # holds its extension); a file path is written as given.
        target = Path(path, self.name) if Path(path).is_dir() else Path(path)
        target.write_bytes(self.content)
        if self.download_mode == 'two':
            Path(target.parent, 'second').write_bytes(self.content)
        return True


RELATIONSHIPS = '2 : InternalValidationError : status.isOk() && relationships'


class UnreadableLinks(File):
    """A cloud file whose reference properties raise, as seen on real Fusion: reading
    childReferences, hasChildReferences, hasOutofDateChildReferences or parentReferences
    raises RELATIONSHIPS. The design itself opens and exports."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __getattribute__(self, name):
        if name in ('childReferences', 'hasChildReferences', 'hasOutofDateChildReferences', 'parentReferences'):
            raise RuntimeError(RELATIONSHIPS)
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name in ('childReferences', 'hasChildReferences', 'hasOutofDateChildReferences', 'parentReferences'):
            return  # set by File.__init__; reading them raises
        super().__setattr__(name, value)


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
        # Real Fusion hangs on a path over 260 characters, even with the \\?\ prefix: never give it one.
        assert len(path) <= 259 and not path.startswith('\\\\?\\'), 'Fusion would hang on ' + path
        if self.file.failures:
            self.file.failures -= 1
            raise RuntimeError('Synthetic export failure')
        if path.endswith('.f3d'):
            write_archive(path)
        else:
            Path(path).write_bytes(step_text(self.file.step_solids))
        return True


PNG = b'\x89PNG\r\n\x1a\n synthetic thumbnail'


def thumbnail(state='finished', data=PNG):
    """A DataObjectFuture: state 'finished', 'processing' or 'failed'; its dataObject saves data."""
    def save(path):
        assert len(path) <= 259 and not path.startswith('\\\\?\\'), 'Fusion would hang on ' + path
        Path(path).write_bytes(data)
        return True
    states = dict(processing=0, finished=1, failed=2)
    return NS(state=states[state], dataObject=NS(saveToFile=save) if state == 'finished' else None)


PENDING = []  # writes the synthetic Fusion does a little later (on doEvents)


def do_events():
    """adsk.doEvents for tests that need Fusion's late work done: runs one pending write."""
    if PENDING:
        PENDING.pop(0)()


class DrawingManager:
    """DrawingExportManager of a synthetic drawing (one per opened document)."""
    def __init__(self, file):
        self.file = file
        self.executes = 0

    def createPDFExportOptions(self, path):
        return NS(filename=path, openPDF=True, sheetRange=0, sheetsToExport=0, useLineWeights=True)

    def execute(self, options):
        path = options.filename
        assert len(path) <= 259 and not path.startswith('\\\\?\\'), 'Fusion would hang on ' + path
        assert options.openPDF is False, 'a PDF viewer would open'
        mode = self.file.pdf_mode
        if mode == 'false':
            return False
        self.executes += 1
        if self.executes == 1 and self.file.first_pdf_empty:
            return True  # as on real Fusion: True, and no file

        def write():
            Path(path).write_bytes(PDF if mode != 'partial' else PDF[:20])
        if mode in ('now', 'partial'):
            write()
        elif mode == 'later':
            PENDING.extend([lambda: None, lambda: None, write])
        return True


class Drawing:
    """The product of an opened drawing: class name Drawing. Reading productType raises, as on
    real Fusion."""
    objectType = 'adsk::drawing::Drawing'

    def __init__(self, file):
        self.exportManager = DrawingManager(file)

    @property
    def productType(self):
        raise RuntimeError('2 : InternalValidationError : adapter')


PDF = b'%PDF-1.4\n% synthetic drawing, no real content\ntrailer\n<<>>\n%%EOF\n'


def write_archive(path):
    """A small zip with the members of a Fusion archive (no real CAD data)."""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Manifest.dat', 'synthetic')
        archive.writestr('FusionAssetName[Active]/Synthetic', 'synthetic')


def step_text(solids=1, complete=True):
    """A STEP file holding this many solids (placeholders, no real geometry)."""
    lines = ["ISO-10303-21;", "HEADER;", "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));", "ENDSEC;", "DATA;"]
    lines += ["#%d=MANIFOLD_SOLID_BREP('',#%d);" % (2 * n + 1, 2 * n + 2) for n in range(solids)]
    lines += ['/* synthetic padding */'] * 100 + ['ENDSEC;']
    if complete:
        lines.append('END-ISO-10303-21;')
    return ('\n'.join(lines) + '\n').encode('ascii')


def body(solid=True, shown=True):
    return NS(isSolid=solid, isLightBulbOn=shown)


def component(identity, bodies=(), occurrences=()):
    """A component with bodies; on a root, occurrences is its allOccurrences (see occurrence())."""
    return NS(id=identity, bRepBodies=Collection(*bodies), allOccurrences=Collection(*occurrences),
              isBodiesFolderLightBulbOn=True)


def occurrence(part, visible=True, bulb=True, parent=None):
    """An occurrence of part; parent is the occurrence it sits in (its assemblyContext)."""
    return NS(component=part, isVisible=visible, isLightBulbOn=bulb, assemblyContext=parent)


class Document:
    """An open document. Made directly in a test it is one the user opened (shown in Fusion);
    Documents.open makes hidden ones, as the add-in asks for."""
    def __init__(self, file, owner=None, visible=True):
        self.dataFile = file
        self.closed = False
        self.isValid = True
        self.pulled = []  # linked documents Fusion opened with this one (and closes with it)
        self.owner = owner  # the Documents collection it is listed in, once opened there
        self.documentReferences = Collection(*([NS()] if file.references else []))
        self.isModified = False
        self.isVisible = visible
        if file.root is not None:
            root = file.root
            parts = {}
            for index in range(root.allOccurrences.count):
                part = root.allOccurrences.item(index).component
                parts[part.id] = part
            components = [root] + list(parts.values())
        else:
            # Root: one solid and one surface body; a visible part holds one hidden solid.
            part = component('part', [body(shown=False)])
            root = component('root', [body(), body(solid=False)], [occurrence(part)])
            components = [root, part]
        self._products = NS(itemByProductType=lambda product: NS(
            exportManager=Manager(file), rootComponent=root,
            allComponents=Collection(*components)) if file.design else None)
        if str(file.fileExtension).lower() == 'f2d':
            # A drawing: its products hold one Drawing with a DrawingExportManager.
            self._products = Collection(Drawing(file))

    @property
    def products(self):
        # Seen on real Fusion: reading a just-opened drawing's products can fail once.
        if self.dataFile.products_flaky:
            self.dataFile.products_flaky -= 1
            raise RuntimeError('2 : InternalValidationError : adapter')
        return self._products

    def close(self, save):
        assert save is False
        if not self.isValid:
            raise RuntimeError('3 : the document is no longer valid')
        if self.dataFile.close_result is False:
            return False
        self.closed, self.isValid = True, False
        if self.owner is not None and self in self.owner.items:
            self.owner.items.remove(self)
        # As on real Fusion: closing a design also closes the linked designs it pulled in.
        for linked in self.pulled:
            if linked.isValid:
                linked.closed, linked.isValid = True, False
                if linked.owner is not None and linked in linked.owner.items:
                    linked.owner.items.remove(linked)
        return True


class Documents(Collection):
    """Application.documents. Opening a design also opens the designs it references (as Fusion
    does for an assembly), unless one of them is open already; all are listed until closed.
    Opening a file that is open already returns that document (as the add-in assumes Fusion
    does). Documents opened here are hidden, and modified at once if the file says so."""
    def open(self, file, visible):
        assert visible is False
        file.calls += 1
        if file.open_error:
            raise RuntimeError(file.open_error)
        for open_ in self.items:
            if open_.dataFile is not None and open_.dataFile.id == file.id:
                return open_
        document = self.opened(file)
        try:
            links = collection_items(file.childReferences)
        except Exception:
            links = []  # a file whose links cannot be listed through DataFile still opens
        for linked in links:
            if not any(open_.dataFile is not None and open_.dataFile.id == linked.id for open_ in self.items):
                document.pulled.append(self.opened(linked))
        return document

    def opened(self, file):
        document = Document(file, self, visible=False)
        document.isModified = bool(file.modified_on_open)
        file.documents.append(document)
        self.items.append(document)
        return document


def collection_items(values):
    return [values.item(index) for index in range(values.count)]


def install():
    if getattr(sys.modules.get('adsk'), '_synthetic', False):
        return
    adsk = ModuleType('adsk')
    adsk._synthetic = True
    adsk.doEvents = lambda: None
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
    # Only members known to exist in every Fusion are defined, so a test cannot pass on a member
    # the real API may lack (a Retry button and its DialogRetry result are left out on purpose).
    adsk.core.MessageBoxButtonTypes = NS(OKButtonType=0, OKCancelButtonType=1,
                                         YesNoButtonType=3, YesNoCancelButtonType=4)
    adsk.core.MessageBoxIconTypes = NS(NoIconIconType=0, QuestionIconType=1, InformationIconType=2,
                                       WarningIconType=3, CriticalIconType=4)
    adsk.core.FutureStates = NS(ProcessingFutureState=0, FinishedFutureState=1, FailedFutureState=2)
    adsk.core.DialogResults = NS(DialogError=-1, DialogOK=0, DialogCancel=1, DialogNo=2, DialogYes=3)
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
        self.tooltip = ''

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

    # Layout as Fusion draws it: an input's name is its label in the dialog's left column and the
    # control sits in the right one, unless the name is empty or isFullWidth is set (both recorded
    # here, so tests can check the layout). A button-style bool input shows its name as the label
    # and its text on the button; until text is set the button repeats the name.
    def addBoolValueInput(self, id, name, is_check_box, resources, value):
        return self._add(NS(id=id, name=name, value=value, isVisible=True, isEnabled=True, isFullWidth=False,
                            isCheckBox=is_check_box, text='' if is_check_box else name, tooltip=''))

    def addBrowserCommandInput(self, id, name, url, minimum_height, maximum_height=0):
        sent = []
        return self._add(NS(id=id, name=name, htmlFileURL=url, isVisible=True, sent=sent, isFullWidth=False,
                            minimumHeight=minimum_height, maximumHeight=maximum_height, tooltip='',
                            sendInfoToHTML=lambda action, data: sent.append((action, data)) or True))

    def addTextBoxCommandInput(self, id, name, text, rows, read_only):
        return self._add(NS(id=id, name=name, text=text, isVisible=True, isFullWidth=False, numRows=rows,
                            isReadOnly=read_only, tooltip=''))

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
