"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import os

import adsk.core
import adsk.fusion
from .core import NonDesign, collection
from .names import fusion_path, long_path
from .staging import Staging


class FusionAdapter:
    def __init__(self, app):
        self.app = app
        self.details = {}  # facts about the last exported design, stored in its manifest entry
        self.staging = None  # set by the Exporter: where Fusion writes before files are moved

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

    def close_opened(self, before):
        """Close the documents opened since `before` (an open_ids() snapshot) that hold no edits.

        Opening an assembly makes Fusion open the designs it links to as well; left open they
        pile up over a long run. Documents the user had open are in `before` and are never
        touched; one that shows unsaved edits is left alone too. Returns how many stay open."""
        left = 0
        for document in list(collection(self.app.documents)):
            identity = document_id(document)
            if identity is None or identity in before:
                continue
            try:
                if document.isModified or document.close(False) is False:
                    left += 1
            except Exception:
                left += 1
        if left:
            self.details['documents_left_open'] = left
        return left

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
        # Opening an already open document may return that same document. Export from it
        # without closing it, unless it holds unsaved edits or another version.
        for existing in collection(self.app.documents):
            if document_id(existing) == str(data_file.id):
                if existing.isModified:
                    raise RuntimeError('This design is open in Fusion with unsaved changes; '
                                       'save or close it, then run again.')
                if int(existing.dataFile.versionNumber) != int(data_file.versionNumber):
                    raise RuntimeError('Another version of this design is open in Fusion; '
                                       'close it, then run again.')
                return self.write(existing, data_file, stem)
        try:
            document = self.app.documents.open(data_file, False)
            if not document:
                raise RuntimeError('Fusion could not open the design')
            try:
                return self.write(document, data_file, stem)
            finally:
                if document.close(False) is False:
                    raise RuntimeError('Fusion could not close the exported document')
        finally:
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
    try:
        if os.path.isfile(long_path(path)):
            os.unlink(long_path(path))  # nothing half-written is left behind
    except OSError:
        pass
    return None


def document_id(document):
    """The cloud file id of an open document, or None (unsaved, or not readable)."""
    try:
        data_file = document.dataFile
        return None if data_file is None else str(data_file.id)
    except Exception:
        return None
