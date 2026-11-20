"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import os

import adsk.core
import adsk.fusion
from .core import NonDesign, collection
from .names import fusion_path, long_path


class FusionAdapter:
    def __init__(self, app):
        self.app = app
        self.details = {}  # facts about the last exported design, stored in its manifest entry

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
            # STEP export leaves out hidden bodies and hidden components: count what it keeps.
            root = design.rootComponent
            shown = {root.id: root}
            for occurrence in collection(root.allOccurrences):
                if occurrence.isVisible:
                    shown[occurrence.component.id] = occurrence.component
            self.details['visible_solid_bodies'] = sum(
                1 for component in shown.values()
                for body in collection(component.bRepBodies) if body.isSolid and body.isLightBulbOn)
        except Exception as error:
            # Counting is for checking only; it never stops an export.
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
        self.details = {}
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
        # Long Windows paths go to Fusion with the \\?\ prefix (names.fusion_path decides).
        if not manager.execute(manager.createFusionArchiveExportOptions(fusion_path(archive))):
            raise RuntimeError('Fusion archive export returned false')
        if not os.path.isfile(long_path(archive)):
            raise RuntimeError('Fusion reported a successful archive export but wrote no file')
        if not manager.execute(manager.createSTEPExportOptions(fusion_path(step))):
            raise RuntimeError('STEP export returned false')
        if not os.path.isfile(long_path(step)):
            raise RuntimeError('Fusion reported a successful STEP export but wrote no file')
        return [archive, step]


def document_id(document):
    """The cloud file id of an open document, or None (unsaved, or not readable)."""
    try:
        data_file = document.dataFile
        return None if data_file is None else str(data_file.id)
    except Exception:
        return None
