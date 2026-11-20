"""Synthetic documented-API surface, with no real CAD data."""
import sys
import weakref
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
        self.latestVersionNumber = version
        self.fileExtension, self.dateModified = extension, 1700000000
        self.linked_error = None  # raised by the archive export made after links were removed
        self.compute = None  # 'missing' or an error message for design.computeAll (design_product)
        linked = [File('Linked part', 'linked-' + identity + ('' if n == 0 else '-%d' % (n + 1)))
                  for n in range(int(references))]
        self.childReferences = Collection(*linked)
        self.references, self.design = references, design
        # DataFile.hasChildReferences / hasOutofDateChildReferences: plain properties.
        self.hasChildReferences = bool(linked)
        self.hasOutofDateChildReferences = False
        # Set to a message: Documents.open raises it, as when a linked design is missing.
        self.open_error = None
        self.step_solids = 1  # solids in the STEP the synthetic exporter writes (of the shown bodies)
        # The STEP options' hidden-bodies properties (isIncludingInvisibleBodies and
        # isIncludingInvisibleComponents, seen on real Fusion's export options): 'both' (offered),
        # 'none' (this Fusion has neither), 'bodies' (only the first), or 'raise' (setting fails).
        self.step_hidden_options = 'both'
        # Solids a STEP with hidden bodies included adds to step_solids; None: the design's
        # switched-off solid bodies (see hidden_solids()).
        self.step_hidden_solids = None
        # DataFile.thumbnail: a DataObjectFuture, finished here (see thumbnail()).
        self.thumbnail = thumbnail()
        self.root = None      # a root component (see component()) to use instead of the default
        # A function making a fresh root component for each opened document (linked_assembly()):
        # removing links changes the document's own copy, never the file's.
        self.make_root = None
        # Called with the document as Document.close(False) closes it (a test can make the
        # synthetic account change there, as a save would).
        self.on_close = None
        self.failures = 0
        self.calls = 0
        self.documents = []
        # DataFile.download(folder, None): how the synthetic Fusion answers ('ok', 'false',
        # 'raise', 'nothing', 'two' files, or 'pdf': the file comes down as a PDF), and what.
        self.download_mode = 'ok'
        self.products_flaky = 0
        # Seen with old or linked designs: Fusion marks a document modified as it opens it
        # (a recompute, an updated reference), although nobody edited it.
        self.modified_on_open = False
        self.close_result = True  # what Document.close returns (Fusion can answer False)
        # Seconds Documents.open blocks Fusion for this file (on the synthetic clock, see clock()):
        # seen on real Fusion, about 135 s for a design "locked in another team".
        self.open_seconds = 0
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


class Weak:
    """A document held weakly (as Fusion's API objects do not keep a closed document alive): no
    reference cycle keeps a document in memory after the add-in lets go of it."""
    @property
    def document(self):
        return None if self._document is None else self._document()

    @document.setter
    def document(self, value):
        self._document = None if value is None else weakref.ref(value)


class Manager(Weak):
    def __init__(self, file, document=None):
        self.file = file
        self.document = document

    def createFusionArchiveExportOptions(self, path):
        # Like Fusion: the archive is always .f3d, whatever extension is asked for.
        return path if path.endswith('.f3d') else path + '.f3d'

    def createSTEPExportOptions(self, path):
        return step_options(path, self.file.step_hidden_options)

    def execute(self, path):
        options = path  # a STEP's options are a str too (step_options), with properties
        # Real Fusion hangs on a path over 260 characters, even with the \\?\ prefix: never give it one.
        assert len(path) <= 259 and not path.startswith('\\\\?\\'), 'Fusion would hang on ' + path
        if self.file.failures:
            self.file.failures -= 1
            raise RuntimeError('Synthetic export failure')
        if path.endswith('.f3d'):
            links = linked_left(self.document)
            if self.document is not None and self.document.dataFile.linked_error is not None and \
                    self.document.links_removed:
                raise self.document.dataFile.linked_error
            write_archive(path, links)
        else:
            solids = self.file.step_solids
            if getattr(options, 'isIncludingInvisibleBodies', False):
                extra = self.file.step_hidden_solids
                solids += hidden_solids(self.document) if extra is None else extra
            Path(path).write_bytes(step_text(solids))
        return True


class OptionError(Exception):
    pass


def step_options(path, mode='both'):
    """STEPExportOptions: filename, plus (mode 'both') isIncludingInvisibleBodies and
    isIncludingInvisibleComponents, both False until set, as seen on real Fusion; 'none' has
    neither, 'bodies' only the first, 'raise' both but setting them fails. A str (the path) as
    well, so tests that look at the paths Fusion was given keep working."""
    class Options(str):
        def __setattr__(self, name, value):
            if mode == 'raise' and name.startswith('isIncluding'):
                raise OptionError('3 : synthetic refusal')
            object.__setattr__(self, name, value)
    options = Options(path)
    object.__setattr__(options, 'filename', path)
    names = dict(both=HIDDEN, bodies=HIDDEN[:1], none=(), **{'raise': HIDDEN})[mode]
    for name in names:
        object.__setattr__(options, name, False)
    return options


HIDDEN = ('isIncludingInvisibleBodies', 'isIncludingInvisibleComponents')


def hidden_solids(document):
    """The switched-off solid bodies of an open document's components (each component once)."""
    if document is None:
        return 0
    return sum(1 for component in document.components for index in range(component.bRepBodies.count)
               if component.bRepBodies.item(index).isSolid and not component.bRepBodies.item(index).isLightBulbOn)


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


class FusionCrashed(BaseException):
    """Fusion itself died. A BaseException, so no `except Exception` in the add-in can hide it."""


def crashed(*args):
    raise FusionCrashed(DRAWING_CRASH)


DRAWING_CRASH = ('Fusion crashed: a drawing was opened or exported through the API (seen on real Fusion: '
                 'an access violation in its sheet export). The add-in must never do this.')


def write_archive(path, links=None):
    """A small zip with the members of a Fusion archive (no real CAD data). links: how many
    linked components the design still had (written into the archive, so a test can tell an
    export with links from one made after they were removed)."""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Manifest.dat', 'synthetic')
        archive.writestr('FusionAssetName[Active]/Synthetic', 'synthetic' if links is None else 'links: %d' % links)


def archive_links(path):
    """The linked components an archive written by the synthetic exporter still had."""
    with zipfile.ZipFile(str(path)) as archive:
        text = archive.read('FusionAssetName[Active]/Synthetic').decode('ascii')
    return int(text.split(': ')[1]) if text.startswith('links') else None


def linked_left(document):
    """How many occurrences of an open document are still linked components (None: no document)."""
    if document is None:
        return None
    root = document.root
    return sum(1 for index in range(root.allOccurrences.count)
               if getattr(root.allOccurrences.item(index), 'isReferencedComponent', False))


class AccountWrite(BaseException):
    """The add-in called an API that writes to the Fusion account (save, saveAs, upload). A
    BaseException: no `except Exception` in the add-in can hide it, and the test fails."""


def account_write(*args, **kwargs):
    raise AccountWrite('The add-in must never save, upload or change anything in the Fusion account.')


# Every breakLink call on any synthetic occurrence (a default run must make none), and, for tests
# of the call order, a log of breakLink, computeAll and (when a test patches adsk.doEvents to
# record into it) doEvents calls. Tests clear both.
BREAK_LINK_CALLS = [0]
CALLS = []


class Occurrence(Weak):
    """An occurrence; referenced (isReferencedComponent) for a linked component. breakLink()
    removes its link in the document's memory, as on real Fusion: the document becomes modified,
    and the linked components inside it (inner) appear in allOccurrences only now. break_result:
    True, False (breakLink answers False), or an error message (breakLink raises it)."""
    def __init__(self, part, referenced=False, parent=None, inner=(), break_result=True, visible=True, bulb=True,
                 seconds=0):
        self.component, self.isReferencedComponent, self.assemblyContext = part, referenced, parent
        self.isVisible, self.isLightBulbOn, self.isValid = visible, bulb, True
        self.inner = list(inner)
        self.break_result = break_result
        self.seconds = seconds  # how long breakLink blocks Fusion (synthetic clock)
        self.document = None
        self.breaks = 0

    def breakLink(self):
        self.breaks += 1
        BREAK_LINK_CALLS[0] += 1
        CALLS.append(('breakLink', self.component.id))
        document = self.document
        assert document is not None and not document.isVisible, 'links removed in a document the user can see'
        CLOCK[0] += self.seconds
        if isinstance(self.break_result, str):
            raise RuntimeError(self.break_result)
        if self.break_result is False:
            return False
        if not self.isReferencedComponent:
            raise RuntimeError('3 : not a linked component')
        self.isReferencedComponent = False
        document.isModified = True
        document.links_removed = True
        for child in self.inner:
            child.assemblyContext = self
            child.document = document
            document.root.allOccurrences.items.append(child)
        self.inner = []
        return True


def linked_assembly(file, parts=3, nested=0, under_local=True, break_result=True, endless=False, seconds=0):
    """Give file the shape of an assembly of linked designs (its documents get a fresh root each):
    parts linked components, each below a local sub-component when under_local (as seen on real
    Fusion), each holding nested linked components that appear once its link is removed. endless:
    every removed link exposes another one (links that never run out)."""
    def make():
        local = Occurrence(component('local-sub', [body()]))
        occurrences = [local]
        for n in range(parts):
            inner = [Occurrence(component('inner-%d-%d' % (n, m), [body()]), referenced=True) for m in range(nested)]
            occurrences.append(Occurrence(component('linked-%d' % n, [body()]), referenced=True,
                                          parent=local if under_local else None, inner=inner,
                                          break_result=break_result, seconds=seconds))
        root = component('root', [body()], occurrences)
        if endless:
            def chain(occurrence, depth=0):
                child = Occurrence(component('deeper-%d' % depth, [body()]), referenced=True)
                original = child.breakLink

                def more():
                    if child.isReferencedComponent and not child.inner:
                        child.inner = [chain(child, depth + 1)]
                    return original()
                child.breakLink = more
                return child
            for occurrence in occurrences[1:]:
                occurrence.inner.append(chain(occurrence))
        return root
    file.make_root = make
    return file


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


def component(identity, bodies=(), occurrences=(), meshes=0):
    """A component with bodies (and meshes mesh bodies); on a root, occurrences is its
    allOccurrences (see occurrence())."""
    return NS(id=identity, bRepBodies=Collection(*bodies), allOccurrences=Collection(*occurrences),
              isBodiesFolderLightBulbOn=True, meshBodies=Collection(*[NS() for _ in range(meshes)]))


def occurrence(part, visible=True, bulb=True, parent=None):
    """An occurrence of part; parent is the occurrence it sits in (its assemblyContext)."""
    return NS(component=part, isVisible=visible, isLightBulbOn=bulb, assemblyContext=parent)


def design_product(file, document, root, components):
    """A document's Design. computeAll records itself in CALLS; file.compute: 'missing' (this
    Fusion has no computeAll) or an error message (computeAll raises it)."""
    design = NS(exportManager=Manager(file, document), rootComponent=root, allComponents=Collection(*components))
    compute = file.compute
    if compute != 'missing':
        def compute_all():
            CALLS.append(('computeAll',))
            if compute:
                raise RuntimeError(compute)
            return True
        design.computeAll = compute_all
    return design


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
        self.links_removed = False
        self.closes = []  # the save argument of each close() call
        # Never called by the add-in: anything that writes to the account fails the test at once.
        self.save = self.saveAs = self.upload = account_write
        if file.make_root is not None:
            root = file.make_root()
            for index in range(root.allOccurrences.count):
                root.allOccurrences.item(index).document = self
            parts = {}
            for index in range(root.allOccurrences.count):
                part = root.allOccurrences.item(index).component
                parts[part.id] = part
            components = [root] + list(parts.values())
        elif file.root is not None:
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
        self.root, self.components = root, components
        me = weakref.ref(self)
        self._products = NS(itemByProductType=lambda product: design_product(file, me(), root, components)
                            if file.design else None)
        if str(file.fileExtension).lower() == 'f2d':
            # A drawing the user opened: anything the add-in reads from it is a crash on export.
            self._products = Collection(NS(exportManager=NS(
                createPDFExportOptions=lambda path: path, execute=crashed)))

    @property
    def products(self):
        # Seen on real Fusion: reading a just-opened drawing's products can fail once.
        if self.dataFile.products_flaky:
            self.dataFile.products_flaky -= 1
            raise RuntimeError('2 : InternalValidationError : adapter')
        return self._products

    def close(self, save):
        self.closes.append(save)
        assert save is False, 'a document must be closed without saving'
        if not self.isValid:
            raise RuntimeError('3 : the document is no longer valid')
        if self.dataFile.close_result is False:
            return False
        if self.dataFile.on_close is not None:
            self.dataFile.on_close(self)
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


CLOCK = [0.0]  # the synthetic Fusion's clock: open() of a slow file moves it on


def clock():
    """A monotonic clock that only Fusion's slow work moves (give it to Exporter.clock)."""
    return CLOCK[0]


class Documents(Collection):
    """Application.documents. Opening a design also opens the designs it references (as Fusion
    does for an assembly), unless one of them is open already; all are listed until closed.
    Opening a file that is open already returns that document (as the add-in assumes Fusion
    does). Documents opened here are hidden, and modified at once if the file says so."""
    def open(self, file, visible):
        assert visible is False
        file.calls += 1
        CLOCK[0] += file.open_seconds
        # On real Fusion, a drawing opened through the API took minutes and its PDF export then
        # crashed Fusion itself: opening one is a test failure, not a modelled behaviour.
        if str(file.fileExtension).lower() == 'f2d':
            raise FusionCrashed(DRAWING_CRASH)
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
    # Only members a real Fusion was seen to have are defined, so a test cannot pass on a member
    # the real API lacks: DialogResults has no DialogRetry, so a Retry button's answer is unknown.
    adsk.core.MessageBoxButtonTypes = NS(OKButtonType=0, OKCancelButtonType=1, RetryCancelButtonType=2,
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


class Event:
    """A Fusion event: handlers added with add() are notified by the test (or the fake)."""
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)
        return True

    def notify(self, args):
        for handler in list(self.handlers):
            handler.notify(args)


class Command:
    """A command as its commandCreated handler gets it: dialog inputs, events, button texts and
    sizes. Command.isOKButtonVisible, okButtonText and cancelButtonText exist on real Fusion
    (probe); doExecute(terminate) ends the command (recorded here); isAutoExecute set during
    commandCreated ends it without showing the dialog."""
    def __init__(self, inputs=None):
        self.commandInputs = inputs if inputs is not None else Inputs()
        self.execute, self.inputChanged, self.validateInputs = Event(), Event(), Event()
        self.incomingFromHTML, self.destroy = Event(), Event()
        self.okButtonText, self.cancelButtonText = 'OK', 'Cancel'
        self.isOKButtonVisible, self.isAutoExecute, self.isRepeatable = True, False, True
        self.sizes, self.executed = {}, []

    def setDialogInitialSize(self, width, height):
        self.sizes['initial'] = (width, height)

    def setDialogMinimumSize(self, width, height):
        self.sizes['minimum'] = (width, height)

    def doExecute(self, terminate):
        self.executed.append(terminate)
        self.execute.notify(NS())
        if terminate:
            self.destroy.notify(NS())
        return True


class OldCommand(Command):
    """A command of a Fusion without isOKButtonVisible (setting it raises)."""
    def __setattr__(self, name, value):
        if name == 'isOKButtonVisible' and value is False:
            raise AttributeError(name)
        super().__setattr__(name, value)


class CommandDefinition:
    """A button definition. execute() starts the command: commandCreated is notified with a new
    Command (made by make_command, so a test can choose an older one). How Fusion answers is
    the test's choice (execute_mode): 'now' (commandCreated inside execute(), returns True),
    'later' (returns True; commandCreated comes when Fusion is idle: queued on later, a
    CustomEvents, run by its pump()), 'never' (returns True, commandCreated never comes),
    'false' (returns False, nothing starts) or 'raise'."""
    def __init__(self, identity, name='', make_command=Command):
        self.id, self.name = identity, name
        self.commandCreated = Event()
        self.make_command = make_command
        self.commands = []
        self.deleted = False
        self.execute_mode, self.later = 'now', None
        self.calls = 0

    def execute(self, inputs=None):
        self.calls += 1
        if self.execute_mode == 'raise':
            raise RuntimeError('3 : synthetic: the command cannot be started')
        if self.execute_mode == 'false':
            return False
        if self.execute_mode == 'never':
            return True
        if self.execute_mode == 'later':
            self.later.queue.append(self.create)
            return True
        self.create()
        return True

    def create(self):
        command = self.make_command()
        self.commands.append(command)
        self.commandCreated.notify(NS(command=command))

    def deleteMe(self):
        self.deleted = True
        return True


class CommandDefinitions:
    """UserInterface.commandDefinitions: addButtonDefinition and itemById."""
    def __init__(self):
        self.items = {}

    def addButtonDefinition(self, identity, name, tooltip='', resources=''):
        if identity in self.items and not self.items[identity].deleted:
            raise RuntimeError('3 : a command definition with this id already exists')
        self.items[identity] = CommandDefinition(identity, name)
        return self.items[identity]

    def itemById(self, identity):
        found = self.items.get(identity)
        return found if found is not None and not found.deleted else None


class CustomEvents:
    """Application.registerCustomEvent / fireCustomEvent / unregisterCustomEvent. As in Fusion,
    a fired event is not handled at once but later, when Fusion is idle: here when the test
    calls pump()."""
    def __init__(self):
        self.events, self.queue = {}, []

    def register(self, name):
        return self.events.setdefault(name, Event())

    def unregister(self, name):
        self.events.pop(name, None)
        return True

    def fire(self, name, info=''):
        if name not in self.events:
            return False
        self.queue.append((name, info))
        return True

    def pump(self):
        """Fusion is idle: handle what was fired, in order (and other work queued here, such as
        a command started with CommandDefinition.execute_mode 'later')."""
        while self.queue:
            item = self.queue.pop(0)
            if callable(item):
                item()
                continue
            name, info = item
            if name in self.events:
                self.events[name].notify(NS(additionalInfo=info))
