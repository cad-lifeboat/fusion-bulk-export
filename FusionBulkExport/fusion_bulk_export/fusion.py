"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import adsk.core
import adsk.fusion
from .core import NonDesign, collection


class FusionAdapter:
    def __init__(self, app):
        self.app = app

    def export(self, data_file, stem):
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
            references = fusion_document.documentReferences.count > 0 or data_file.childReferences.count > 0
            archive = str(stem) + ('.f3z' if references else '.f3d')
            step = str(stem) + '.step'
            manager = design.exportManager
            if not manager.execute(manager.createFusionArchiveExportOptions(archive)):
                raise RuntimeError('Fusion archive export returned false')
            if not manager.execute(manager.createSTEPExportOptions(step)):
                raise RuntimeError('STEP export returned false')
            return [archive, step]
        finally:
            if document.close(False) is False:
                raise RuntimeError('Fusion could not close the exported document')
