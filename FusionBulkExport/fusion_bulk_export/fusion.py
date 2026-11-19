"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import os

import adsk.core
import adsk.fusion
from .core import NonDesign, collection


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
        # Distinct solid bodies over all components: what the STEP should carry.
        self.details['solid_bodies'] = sum(
            1 for component in collection(design.allComponents)
            for body in collection(component.bRepBodies) if body.isSolid)

    def export(self, data_file, stem):
        self.details = {}
        # Opening an already open document may return that same document. Preserve it.
        for existing in collection(self.app.documents):
            if existing.dataFile and existing.dataFile.id == data_file.id:
                raise RuntimeError('Close this design in Fusion before exporting it.')
        document = self.app.documents.open(data_file, False)
        if not document:
            raise RuntimeError('Fusion could not open the design')
        try:
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
            if not manager.execute(manager.createFusionArchiveExportOptions(archive)):
                raise RuntimeError('Fusion archive export returned false')
            if not os.path.isfile(archive):
                raise RuntimeError('Fusion reported a successful archive export but wrote no file')
            if not manager.execute(manager.createSTEPExportOptions(step)):
                raise RuntimeError('STEP export returned false')
            if not os.path.isfile(step):
                raise RuntimeError('Fusion reported a successful STEP export but wrote no file')
            return [archive, step]
        finally:
            if document.close(False) is False:
                raise RuntimeError('Fusion could not close the exported document')
