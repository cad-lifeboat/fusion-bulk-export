"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import importlib
import os
from pathlib import Path
import shutil
import time

import adsk.core
import adsk.fusion
from .core import NonDesign, NotSaved, collection, disk_full
from .names import fusion_path, long_path
from .staging import Staging

# Fusion's own types other than designs and drawings: DataFile.download refuses them ("file
# type not available for download", seen on real Fusion for .f2d, .fsch, .fbrd and .flbr) and
# there is no export for them in the API, so they are skipped without asking.
FUSION_NATIVE = {'f2d', 'f2t', 'fsch', 'fbrd', 'flbr', 'fprj', 'cam360'}
NATIVE_REASON = 'Fusion cannot export this file type through its API'
PDF_WAIT_SECONDS = 20     # how long a drawing's PDF is waited for after Fusion's export returns
PRODUCT_TRIES = 5         # a just-opened drawing's products can fail to read the first time


# Cloud ids of documents this add-in opened (hidden) and Fusion did not close yet. Kept for the
# whole Fusion session, not one run, so a later run still knows they are the add-in's own.
LEFT_OPEN = set()


class FusionAdapter:
    def __init__(self, app):
        self.app = app
        self.details = {}  # facts about the last exported design, stored in its manifest entry
        self.staging = None  # set by the Exporter: where Fusion writes before files are moved
        self.pdf_broken = None  # why drawings are not tried any more in this run, once one failed
        # Documents this add-in opened and could not close yet (see LEFT_OPEN). They are its own
        # (opened hidden, never shown to the user), so they may be closed or exported from
        # later even though they are open "before" a later export.
        self.left_open = LEFT_OPEN

    def references(self, data_file):
        """Designs this one references (linked components, derives), by cloud id and name.

        The Fusion API can only write a design's own archive (.f3d), never a .f3z bundle with
        its references, so the links are recorded instead; each referenced design in the
        account is exported in its own right.
        """
        return [dict(id=str(child.id), name=child.name) for child in collection(data_file.childReferences)]

    def observe(self, design, data_file):
        """Called with each opened design before export; records facts for the manifest."""
        try:
            # Distinct solid bodies over all components, hidden or not (the .f3d keeps all).
            self.details['solid_bodies'] = sum(
                1 for component in collection(design.allComponents)
                for body in collection(component.bRepBodies) if body.isSolid)
        except Exception as error:
            # Counting is for checking only; it never stops an export.
            self.details['count_error'] = str(error)
        try:
            self.details.update(visible_counts(design.rootComponent))
        except Exception as error:
            self.details['count_error'] = str(error)

    def open_ids(self):
        """Cloud ids of the documents open now. A document without a cloud file (new and never
        saved) has no id; it is never one this add-in opened, so it is simply not listed."""
        ids = set()
        for document in collection(self.app.documents):
            identity = document_id(document)
            if identity is not None:
                ids.add(identity)
        return ids

    def close(self, document):
        """Close a document this add-in opened, without saving. Never raises; False when Fusion
        did not close it (it is then remembered in left_open and tried again later)."""
        identity = document_id(document)
        try:
            closed = document.close(False) is not False
        except Exception:
            closed = False
        if identity is not None:
            (self.left_open.discard if closed else self.left_open.add)(identity)
        return closed

    def close_opened(self, before):
        """Close the documents this add-in caused to be opened since `before` (an open_ids()
        snapshot), without saving. Never raises.

        Opening an assembly or a drawing makes Fusion open the designs it links to as well; left
        open they pile up over a long run. They are opened hidden, so the user cannot have edited
        them: Fusion may mark them modified on open (a recompute, an updated reference), and they
        are closed all the same. Documents the user had open are in `before` and never touched;
        a visible document that appeared meanwhile (the user opened it) is left alone too.
        Returns how many documents this add-in opened are still open."""
        try:
            for document in list(collection(self.app.documents)):
                identity = document_id(document)
                if identity is None or (identity in before and identity not in self.left_open):
                    continue
                if visible(document):
                    continue  # shown in Fusion: the user's, whatever its state
                self.close(document)
            still = self.open_ids() & self.left_open
        except Exception:
            still = set(self.left_open)
        self.left_open &= still
        if still:
            self.details['documents_left_open'] = len(still)
        return len(still)

    def acquire(self, data_file, what, failure):
        """(document, opened) for exporting data_file. A document already open for it is used
        as it is and never closed (opened False): the user's, if it has no unsaved edits and is
        the same version (else failure(plain reason) is raised); or one this add-in left open,
        which is closed and opened again. Otherwise the file is opened hidden (opened True)."""
        identity = str(data_file.id)
        for existing in list(collection(self.app.documents)):
            if document_id(existing) != identity:
                continue
            if identity in self.left_open and not visible(existing):
                if self.close(existing):
                    break  # opened afresh below
                if same_version(existing, data_file):
                    return existing, True  # still this add-in's own; closing is tried again after
                raise failure('Fusion could not close an older version of this %s; restart Fusion, '
                              'then run again.' % what)
            if existing.isModified:
                raise failure('This %s is open in Fusion with unsaved changes; save or close it, then run again.'
                              % what)
            if not same_version(existing, data_file):
                raise failure('Another version of this %s is open in Fusion; close it, then run again.' % what)
            return existing, False
        before = self.open_ids()
        document = self.app.documents.open(data_file, False)
        if not document:
            raise failure('Fusion could not open the %s' % what)
        # Fusion may hand back a document that was open already: that one is not closed.
        return document, identity not in before or identity in self.left_open

    def download(self, data_file, target):
        """Save a file that is not a design to target, its readable final path. Uploaded files
        (PDF, images, STEP...) come down as they are; a drawing is saved as a PDF of its sheets;
        electronics and other Fusion-only types cannot be had through the API and are skipped
        without asking Fusion. Raises NotSaved with the reason when nothing could be saved.
        Reads only: nothing in the account changes."""
        extension = str(data_file.fileExtension or '').lower().strip('.')
        if extension == 'f2d':
            return self.drawing_pdf(data_file, target)
        if extension in FUSION_NATIVE:
            raise NotSaved('%s (.%s)' % (NATIVE_REASON, extension))
        staging = self.stage()
        # A short file path in a fresh folder: Fusion writes exactly that file (it would hang on
        # a long path, and the folder form writes the file under its cloud name, which can be long).
        folder = staging.fresh_folder()
        staged = os.path.join(folder, 'download' + Path(str(target)).suffix)
        try:
            try:
                result = data_file.download(fusion_path(staged), None)
            except Exception as error:
                if disk_full(error):
                    raise  # stops the run, like any write to a full disk
                raise NotSaved(reason(extension, 'Fusion could not download it: %s' % (error or type(error).__name__)))
            if result is False:
                raise NotSaved(reason(extension, 'Fusion did not download it'))
            if not os.path.isfile(long_path(staged)):
                found = []
                for place, _, names in os.walk(long_path(folder)):
                    found += [os.path.join(place, name) for name in names]
                if not found:
                    raise NotSaved(reason(extension, 'Fusion reported a download but wrote no file'))
                if len(found) > 1:
                    raise NotSaved('Fusion downloaded %d files for it; not saved' % len(found))
                staged = found[0]
            return staging.place(staged, target)
        finally:
            shutil.rmtree(long_path(folder), ignore_errors=True)

    def drawing_pdf(self, data_file, target):
        """A drawing as a PDF: opened (hidden), exported through its DrawingExportManager to a
        short staging path, then moved to target. Fusion may write the PDF a little after
        execute() returns, so the file is waited for (PDF_WAIT_SECONDS). If one drawing's PDF
        never appears, the others in this run are not tried (each would cost that wait). A drawing
        already open in Fusion is exported from as it is and left open; one with unsaved edits, or
        another version, is skipped with the reason (like a design)."""
        if self.pdf_broken:
            raise NotSaved(self.pdf_broken)
        before = self.open_ids()
        document, opened = None, False
        try:
            try:
                document, opened = self.acquire(data_file, 'drawing', NotSaved)
            except NotSaved:
                raise
            except Exception as error:
                raise NotSaved('Fusion could not open the drawing: %s' % (error or type(error).__name__))
            drawing = drawing_product(document)
            if drawing is None:
                raise NotSaved('%s (.f2d: no drawing found in the opened file)' % NATIVE_REASON)
            staging = self.stage()
            staged = staging.path('.pdf')
            try:
                manager = drawing.exportManager
                options = manager.createPDFExportOptions(fusion_path(staged))
                if hasattr(options, 'openPDF'):
                    options.openPDF = False  # never open a viewer during an export
                result = manager.execute(options)
            except Exception as error:
                raise NotSaved('%s (.f2d; PDF export failed: %s)' % (NATIVE_REASON, error or type(error).__name__))
            if result is False:
                raise NotSaved('%s (.f2d; PDF export returned false)' % NATIVE_REASON)
            if not wait_for_file(staged, PDF_WAIT_SECONDS):
                self.pdf_broken = 'Fusion did not write the drawing PDF'
                raise NotSaved(self.pdf_broken)
            return staging.place(staged, target)
        finally:
            if document is not None and opened:
                self.close(document)
            self.close_opened(before)  # a drawing opens the designs it shows, too

    def export(self, data_file, stem):
        """Write <stem>.f3d and <stem>.step; returns their paths, plus <stem>.png when the
        design's thumbnail was ready by the time the files were written."""
        self.details = {}
        # Asked for first: Fusion fetches it while the design opens and exports; never waited for.
        future = thumbnail_future(data_file)
        paths = self.export_design(data_file, stem)
        picture = save_thumbnail(future, str(stem) + '.png', self.stage())
        return paths + [picture] if picture else paths

    def stage(self):
        if self.staging is None:
            import tempfile
            self.staging = Staging(tempfile.gettempdir())
        return self.staging

    def export_design(self, data_file, stem):
        before = self.open_ids()
        document, opened = None, False
        try:
            document, opened = self.acquire(data_file, 'design', RuntimeError)
            return self.write(document, data_file, stem)
        finally:
            # Never raises: a document Fusion would not close does not undo a good export; it
            # is recorded (documents_left_open) and closed later when possible.
            if document is not None and opened:
                self.close(document)
            # Also when opening or exporting failed: linked designs may be open by now.
            self.close_opened(before)

    def write(self, document, data_file, stem):
        design = adsk.fusion.Design.cast(document.products.itemByProductType('DesignProductType'))
        if not design:
            raise NonDesign('No Fusion Design product (drawing or CAM-only file)')
        fusion_document = adsk.fusion.FusionDocument.cast(document)
        if not fusion_document:
            raise NonDesign('Not a Fusion document')
        self.observe(design, data_file)
        # Fusion writes .f3d whatever extension is asked for (".f3z" becomes ".f3z.f3d").
        archive = str(stem) + '.f3d'
        step = str(stem) + '.step'
        manager = design.exportManager
        # Fusion hangs on long paths: it writes to short staging paths, then the files move.
        staging = self.stage()
        staged_archive, staged_step = staging.path('.f3d'), staging.path('.step')
        if not manager.execute(manager.createFusionArchiveExportOptions(fusion_path(staged_archive))):
            raise RuntimeError('Fusion archive export returned false')
        if not os.path.isfile(long_path(staged_archive)):
            raise RuntimeError('Fusion reported a successful archive export but wrote no file')
        if not manager.execute(manager.createSTEPExportOptions(fusion_path(staged_step))):
            raise RuntimeError('STEP export returned false')
        if not os.path.isfile(long_path(staged_step)):
            raise RuntimeError('Fusion reported a successful STEP export but wrote no file')
        staging.place(staged_archive, archive)
        staging.place(staged_step, step)
        return [archive, step]


def shown_solids(component):
    """Solid bodies of a component that are switched on (hidden bodies are not in the STEP)."""
    if not getattr(component, 'isBodiesFolderLightBulbOn', True):
        return 0
    return sum(1 for body in collection(component.bRepBodies) if body.isSolid and body.isLightBulbOn)


def occurrence_shown(occurrence):
    """Whether an occurrence is shown: visible itself, and every occurrence above it (its
    assemblyContext chain) has its light bulb on. Fusion's isVisible may already include the
    parents; checking them too costs little and is right either way."""
    if not occurrence.isVisible:
        return False
    parent = getattr(occurrence, 'assemblyContext', None)
    while parent is not None:
        if not getattr(parent, 'isLightBulbOn', True) or not getattr(parent, 'isVisible', True):
            return False
        parent = getattr(parent, 'assemblyContext', None)
    return True


def visible_counts(root):
    """What a STEP export can be expected to hold: visible_solid_bodies counts each shown
    occurrence of a component once (two instances of a part are two solids), plus the root's own
    bodies; visible_solid_bodies_distinct counts each shown component once (a STEP writer may
    write a part used several times only once)."""
    per_occurrence = shown_solids(root)
    components = {}
    for occurrence in collection(root.allOccurrences):
        if occurrence_shown(occurrence):
            component = occurrence.component
            solids = shown_solids(component)
            per_occurrence += solids
            components[getattr(component, 'id', id(component))] = solids
    return dict(visible_solid_bodies=per_occurrence,
                visible_solid_bodies_distinct=shown_solids(root) + sum(components.values()))


def drawing_product(document):
    """The Drawing product of an opened drawing document, or None. Reading products right after
    opening can fail once ("InternalValidationError") while the drawing loads: tried again."""
    products = None
    for attempt in range(PRODUCT_TRIES):
        try:
            products = list(collection(document.products))
            break
        except Exception:
            adsk.doEvents()
            time.sleep(0.5)
    if products is None:
        return None
    try:
        cast = importlib.import_module('adsk.drawing').Drawing.cast
    except Exception:
        cast = None
    for product in products:
        try:
            drawing = cast(product) if cast else None
        except Exception:
            drawing = None
        if drawing:
            return drawing
        manager = getattr(product, 'exportManager', None)
        if manager is not None and hasattr(manager, 'createPDFExportOptions'):
            return product
    return None


def wait_for_file(path, seconds):
    """Wait (letting Fusion work) until path exists with a size that stopped changing."""
    deadline = time.monotonic() + seconds
    size = -1
    while True:
        try:
            now_size = os.path.getsize(long_path(path))
        except OSError:
            now_size = -1
        if now_size > 0 and now_size == size:
            return True
        size = now_size
        if time.monotonic() >= deadline:
            return now_size > 0
        adsk.doEvents()
        time.sleep(0.25)


def reason(extension, detail):
    """Why another file was not saved: for Fusion's own types, that the API cannot export them."""
    if extension in FUSION_NATIVE:
        return '%s (.%s; %s)' % (NATIVE_REASON, extension, detail[0].lower() + detail[1:])
    return detail


def thumbnail_future(data_file):
    """DataFile.thumbnail (a DataObjectFuture: Fusion fetches the picture in the background),
    or None. Never raises."""
    try:
        return data_file.thumbnail
    except Exception:
        return None


def save_thumbnail(future, path, staging):
    """Write the thumbnail to path if it has arrived; the path, or None. Never waits and never
    raises: a thumbnail is a convenience, so anything unexpected means no thumbnail."""
    staged = None
    try:
        if future is None:
            return None
        finished = getattr(getattr(adsk.core, 'FutureStates', None), 'FinishedFutureState', None)
        if finished is not None and getattr(future, 'state', finished) != finished:
            return None  # still being fetched (or failed): skip, do not wait
        picture = future.dataObject
        if picture is None:
            return None
        staged = staging.path('.png')  # Fusion writes to a short path; the file then moves
        saved = False
        if hasattr(picture, 'saveToFile'):
            saved = picture.saveToFile(fusion_path(staged))
        elif hasattr(picture, 'getAsBase64String'):
            import base64
            with open(long_path(staged), 'wb') as stream:
                stream.write(base64.b64decode(picture.getAsBase64String()))
            saved = True
        if saved and os.path.isfile(long_path(staged)) and os.path.getsize(long_path(staged)) > 0:
            return staging.place(staged, path)
    except Exception:
        pass
    # Only the staged file can be half-written; whatever is at path (an earlier thumbnail, or a
    # file the user put there) is never removed.
    try:
        if staged is not None and os.path.isfile(long_path(staged)):
            os.unlink(long_path(staged))
    except OSError:
        pass
    return None


def visible(document):
    """Whether a document is shown in Fusion (so the user may be working in it). Unknown counts
    as shown: such a document is never closed."""
    try:
        return bool(getattr(document, 'isVisible', True))
    except Exception:
        return True


def same_version(document, data_file):
    try:
        return int(document.dataFile.versionNumber) == int(data_file.versionNumber)
    except Exception:
        return False


def document_id(document):
    """The cloud file id of an open document, or None (unsaved, or not readable)."""
    try:
        data_file = document.dataFile
        return None if data_file is None else str(data_file.id)
    except Exception:
        return None
