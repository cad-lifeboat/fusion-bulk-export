"""Adapter using Autodesk's public Fusion API. Imported only inside Fusion."""
import os
from pathlib import Path
import shutil
import time

import adsk.core
import adsk.fusion
from .core import (DRAWING_REASON, RECIPE_BOTH, RECIPE_COMPUTE, RECIPE_INNERMOST, UNSUPPORTED, native_reason,
                   NonDesign, NotSaved, OpenInFusion, collection, disk_full, is_drawing)
from .names import LINKED_SUFFIX, fusion_path, long_path
from .staging import Staging

# Fusion's own types other than designs (core.UNSUPPORTED): the exporter records them without
# asking Fusion; download() refuses them too, in case it is ever handed one.
FUSION_NATIVE = UNSUPPORTED | {'f2d'}
PRODUCT_TRIES = 5         # a just-opened document's products can fail to read the first time


# Cloud ids of documents this add-in opened (hidden) and Fusion did not close yet. Kept for the
# whole Fusion session, not one run, so a later run still knows they are the add-in's own.
LEFT_OPEN = set()
# Cloud ids of documents whose links this add-in removed in memory (see linked_copy) and that are
# not closed yet. Such a document is never exported from again: its export would not be the
# design as it is in the account.
BROKEN = set()

# The experimental copy of an assembly with its linked parts included (linked_copy), written only
# while the testing switch core.LINKED_COPIES_ENV is on. Not offered to users: copies written by
# the first recipe crashed Fusion when opened (File > Open and the import API alike), and the
# recipes here (core.LINK_RECIPES) each opened once, on one assembly. Fusion also refuses
# breakLink for some occurrences ("essential dependent instances"); that copy is then skipped.
LINK_PASSES = 10          # at most this many passes over the occurrences (breaking one can expose more)
SETTLE_EVENTS = 5         # adsk.doEvents calls after design.computeAll(), before the copy is exported
LINK_SLOW_SECONDS = 120   # removing the links of one design taking longer is recorded as slow
OPEN_IN_FUSION = 'open in Fusion; close it to get the copy with linked parts'
LINKS_UNKNOWN = "Fusion could not list this design's linked components"


class FusionAdapter:
    def __init__(self, app):
        self.app = app
        self.details = {}  # facts about the last exported design, stored in its manifest entry
        self.staging = None  # set by the Exporter: where Fusion writes before files are moved
        # Documents this add-in opened and could not close yet (see LEFT_OPEN). They are its own
        # (opened hidden, never shown to the user), so they may be closed or exported from
        # later even though they are open "before" a later export.
        self.left_open = LEFT_OPEN
        self.broken = BROKEN
        # Testing only: the recipe (core.LINK_RECIPES) for the experimental
        # "<stem> (with linked parts, experimental).f3d" of a design with linked components, or
        # None (off, the default). The Exporter sets it from core.LINKED_COPIES_ENV or the job.
        self.linked_copies = None
        self.clock = time.monotonic

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
        did not close it (it is then remembered in left_open and tried again later). A document
        that is gone already (closing a design also closes the linked designs it pulled in) is
        closed, not an error."""
        identity = document_id(document)
        if not valid(document):
            closed = True
        else:
            try:
                closed = document.close(False) is not False
            except Exception:
                closed = not valid(document)
        if identity is not None:
            (self.left_open.discard if closed else self.left_open.add)(identity)
            if closed:
                self.broken.discard(identity)
        return closed

    def close_opened(self, before):
        """Close the documents this add-in caused to be opened since `before` (an open_ids()
        snapshot), without saving. Never raises.

        Opening an assembly makes Fusion open the designs it links to as well; left
        open they pile up over a long run. They are opened hidden, so the user cannot have edited
        them: Fusion may mark them modified on open (a recompute, an updated reference), and they
        are closed all the same. Documents the user had open are in `before` and never touched;
        a visible document that appeared meanwhile (the user opened it) is left alone too.
        Documents are told apart only by their cloud file id (dataFile.id), never by creationId
        or object identity (neither is stable across calls on real Fusion); a document without a
        cloud file (a new, unsaved one) is never closed. One that closed meanwhile (with the
        design that pulled it in) is skipped.
        Returns how many documents this add-in opened are still open."""
        try:
            for document in list(collection(self.app.documents)):
                identity = document_id(document)
                if identity is None or (identity in before and identity not in self.left_open):
                    continue
                if not valid(document):
                    continue  # closed already, with the design that pulled it in
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

    def acquire(self, data_file, what, failure, blocked=None):
        """(document, opened) for exporting data_file. A document already open for it is used
        as it is and never closed (opened False): the user's, if it has no unsaved edits and is
        the same version (else blocked(plain reason) is raised, default failure); or one this
        add-in left open, which is closed and opened again. Otherwise the file is opened hidden
        (opened True); failure(reason) when Fusion hands back nothing. A drawing is never
        opened: exporting one crashed Fusion (see core.DRAWING_REASON)."""
        if is_drawing(data_file):
            raise failure(DRAWING_REASON)
        blocked = blocked or failure
        identity = str(data_file.id)
        for existing in list(collection(self.app.documents)):
            if document_id(existing) != identity:
                continue
            if identity in self.broken and not visible(existing):
                # This add-in's own hidden copy, with its links removed: never exported from.
                if self.close(existing):
                    break  # opened afresh below
                raise blocked('Fusion could not close a hidden copy of this %s that had its links removed; '
                              'restart Fusion, then run again.' % what)
            if identity in self.left_open and not visible(existing):
                if self.close(existing):
                    break  # opened afresh below
                if same_version(existing, data_file):
                    return existing, True  # still this add-in's own; closing is tried again after
                raise blocked('Fusion could not close an older version of this %s; restart Fusion, '
                              'then run again.' % what)
            if existing.isModified:
                raise blocked('This %s is open in Fusion with unsaved changes; save or close it, then run again.'
                              % what)
            if not same_version(existing, data_file):
                raise blocked('Another version of this %s is open in Fusion; close it, then run again.' % what)
            return existing, False
        before = self.open_ids()
        document = self.app.documents.open(data_file, False)
        if not document:
            raise failure('Fusion could not open the %s' % what)
        # Fusion may hand back a document that was open already: that one is not closed.
        return document, identity not in before or identity in self.left_open

    def download(self, data_file, target):
        """Save a file that is not a design to target, its readable final path. Uploaded files
        (PDF, images, STEP...) come down as they are; drawings, electronics and other Fusion-only
        types cannot be had through the API and are refused without asking Fusion (the exporter
        records them as unsupported before getting here). Raises NotSaved with the reason when nothing could be saved.
        Reads only: nothing in the account changes."""
        extension = str(data_file.fileExtension or '').lower().strip('.')
        if is_drawing(data_file):
            raise NotSaved(DRAWING_REASON)
        if extension in FUSION_NATIVE:
            raise NotSaved(native_reason(extension))
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

    def export(self, data_file, stem):
        """Write <stem>.f3d and <stem>.step; returns their paths, plus <stem>.png when the
        design's thumbnail was ready by the time the files were written."""
        self.details = {}
        # Asked for first: Fusion fetches it while the design opens and exports; never waited for.
        future = thumbnail_future(data_file)
        try:
            paths = self.export_design(data_file, stem)
            picture = save_thumbnail(future, str(stem) + '.png', self.stage())
        finally:
            future = None  # the picture Fusion fetched goes with it, whatever happened
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
            document, opened = self.acquire(data_file, 'design', RuntimeError, OpenInFusion)
            return self.write(document, data_file, stem, opened)
        finally:
            # Never raises: a document Fusion would not close does not undo a good export; it
            # is recorded (documents_left_open) and closed later when possible. Always without
            # saving (close(False)), also after its links were removed: nothing is ever saved.
            if document is not None and opened:
                if not self.close(document) and self.details.get('links_broken'):
                    copy = self.details.setdefault('linked_copy', {})
                    copy['close_failed'] = True
            # Not held past this design (an exception's traceback keeps this frame alive).
            document = None
            # Also when opening or exporting failed: linked designs may be open by now.
            self.close_opened(before)

    def write(self, document, data_file, stem, opened=False):
        """Export an open document: <stem>.f3d and <stem>.step as the design is, then (only while the
        testing switch is on) for a design with linked components the experimental copy with its
        linked parts (linked_copy). When it raises, its
        variables (design, export manager) are dropped by the exporter (core.forget_frames) once
        the error is recorded."""
        design = adsk.fusion.Design.cast(read_products(document).itemByProductType('DesignProductType'))
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
        # Only now, after the design's own files are written: from here on the document in memory
        # is no longer the design as it is in the account.
        return [archive, step] + self.linked_copy(document, opened, design, stem)

    def linked_copy(self, document, opened, design, stem):
        """Records how many linked components (occurrences with isReferencedComponent) the design
        has. Then, only while the testing switch is on (self.linked_copies, a recipe): remove
        every link in memory (break_links with that recipe, at most LINK_PASSES passes; for the
        compute recipes then settle) and write the now self-contained design as
        "<stem> (with linked parts, experimental).f3d". Returns [its path], or [] (with the reason
        in details['linked_copy'] when the switch is on). Off, nothing else is done: no link is
        removed and the document is left as it opened.

        Safety: only on a hidden document this add-in opened for this export (opened, never one
        the user has open, which would be left modified); nothing is saved (the document is
        closed with close(False) by export_design, whatever happens here; links_broken tells the
        exporter to check that the account is unchanged). A failure here never undoes the
        design's own files: only a full disk is raised."""
        try:
            found = referenced_occurrences(design)
        except Exception:
            self.details['linked_occurrences'] = None
            if self.linked_copies:
                self.details['linked_copy'] = dict(status='skipped', reason=LINKS_UNKNOWN)
            return []
        self.details['linked_occurrences'] = len(found)
        if not found:
            return []
        found = None
        recipe = self.linked_copies
        if not recipe:
            return []
        if not opened or visible(document):
            # The user's open document: removing its links would leave it modified in front of them.
            self.details['linked_copy'] = dict(status='skipped', reason=OPEN_IN_FUSION, retry=True)
            return []
        copy = self.details['linked_copy'] = dict(status='skipped', reason=None, recipe=recipe)
        self.details['links_broken'] = True  # from the first breakLink on: the exporter checks the account
        identity = document_id(document)
        if identity is not None:
            self.broken.add(identity)
        began = self.clock()
        problem, passes, broken = break_links(design, recipe)
        if not problem and recipe in (RECIPE_COMPUTE, RECIPE_BOTH):
            copy['computed'] = settle(design)
        seconds = self.clock() - began
        copy.update(passes=passes, broken=broken, seconds=round(seconds, 1))
        if seconds > LINK_SLOW_SECONDS:
            copy['slow'] = True
        if problem:
            copy['reason'] = problem
            return []
        target = str(stem) + LINKED_SUFFIX
        staging = self.stage()
        staged = staging.path('.f3d')
        try:
            manager = design.exportManager
            if not manager.execute(manager.createFusionArchiveExportOptions(fusion_path(staged))):
                copy['reason'] = 'Fusion did not write the copy with linked parts (its export returned false)'
                return []
            if not os.path.isfile(long_path(staged)):
                copy['reason'] = 'Fusion reported the copy with linked parts written but wrote no file'
                return []
            staging.place(staged, target)
        except Exception as error:
            if disk_full(error):
                raise
            copy['reason'] = 'Fusion could not write the copy with linked parts: %s' % (str(error) or type(error).__name__)
            return []
        finally:
            if copy['reason'] is not None:
                try:
                    os.unlink(long_path(staged))
                except OSError:
                    pass
        copy.update(status='saved', reason=None)
        return [target]


def referenced_occurrences(design):
    """Every occurrence in the design, nested ones included (rootComponent.allOccurrences), that is
    a linked component (isReferencedComponent). On real Fusion the links of an assembly can all
    sit below local sub-components, so the top-level occurrences alone are not enough."""
    return [occurrence for occurrence in collection(design.rootComponent.allOccurrences)
            if getattr(occurrence, 'isReferencedComponent', False)]


def outermost(occurrences):
    """The linked occurrences with no linked occurrence above them (their assemblyContext
    chain): removing an outer link can change what is inside it, so the inner ones are left for
    the next pass, read afresh."""
    chosen = []
    for occurrence in occurrences:
        if not valid(occurrence):
            continue
        parent, inner = getattr(occurrence, 'assemblyContext', None), False
        while parent is not None:
            if getattr(parent, 'isReferencedComponent', False):
                inner = True
                break
            parent = getattr(parent, 'assemblyContext', None)
        if not inner:
            chosen.append(occurrence)
    return chosen


def depth(occurrence):
    """How many occurrences an occurrence sits in (its assemblyContext chain)."""
    count, parent = 0, getattr(occurrence, 'assemblyContext', None)
    while parent is not None:
        count += 1
        parent = getattr(parent, 'assemblyContext', None)
    return count


def innermost(occurrences):
    """The valid linked occurrences, deepest first (in listed order within one depth)."""
    return sorted((occurrence for occurrence in occurrences if valid(occurrence)), key=depth, reverse=True)


def break_links(design, recipe=RECIPE_BOTH, passes=None):
    """Remove every link of an open design in memory: (problem or None, passes made, links
    removed). Pass after pass until no linked occurrence is left, at most LINK_PASSES; a breakLink
    that raises or returns False ends it with the reason. Never raises. Nothing is saved: only
    the document in memory changes.

    recipe (core.LINK_RECIPES): RECIPE_INNERMOST and RECIPE_BOTH break the deepest linked
    occurrence first, with adsk.doEvents after each breakLink (an occurrence no longer valid by
    then waits for the next pass, read afresh); RECIPE_COMPUTE breaks the outermost ones
    (outermost) with adsk.doEvents after each pass. settle() is the caller's."""
    passes = LINK_PASSES if passes is None else passes
    inner_first = recipe in (RECIPE_INNERMOST, RECIPE_BOTH)
    removed = 0
    for number in range(1, passes + 1):
        try:
            found = referenced_occurrences(design)
            left = innermost(found) if inner_first else outermost(found)
            found = None
        except Exception as error:
            return 'Fusion could not list the linked components: %s' % (str(error) or type(error).__name__), \
                number - 1, removed
        if not left:
            return None, number - 1, removed
        for occurrence in left:
            if inner_first and not valid(occurrence):
                continue
            try:
                result = occurrence.breakLink()
            except Exception as error:
                return 'Fusion could not remove a link: %s' % (str(error) or type(error).__name__), number, removed
            if result is False:
                return 'Fusion did not remove a link (breakLink returned false)', number, removed
            removed += 1
            if inner_first:
                adsk.doEvents()
        left = None
        if not inner_first:
            adsk.doEvents()
    try:
        remaining = len(referenced_occurrences(design))
    except Exception as error:
        return 'Fusion could not list the linked components: %s' % (str(error) or type(error).__name__), \
            passes, removed
    if remaining:
        return '%d links were still there after %d passes' % (remaining, passes), passes, removed
    return None, passes, removed


def settle(design):
    """After the links are removed (compute recipes): design.computeAll(), when this Fusion has
    it and it works, then SETTLE_EVENTS adsk.doEvents. Returns True when computeAll ran, else the
    reason it did not. Never raises."""
    computed = True
    if not hasattr(design, 'computeAll'):
        computed = 'this Fusion has no design.computeAll'
    else:
        try:
            design.computeAll()
        except Exception as error:
            computed = 'design.computeAll failed: %s' % (str(error) or type(error).__name__)
    for _ in range(SETTLE_EVENTS):
        adsk.doEvents()
    return computed


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


def read_products(document):
    """document.products. Reading it right after opening can fail ("InternalValidationError")
    while the document loads: tried PRODUCT_TRIES times, then the error."""
    for attempt in range(PRODUCT_TRIES):
        try:
            return document.products
        except Exception:
            if attempt == PRODUCT_TRIES - 1:
                raise
            adsk.doEvents()
            time.sleep(0.5)


def reason(extension, detail):
    """Why another file was not saved: for Fusion's own types, that the API cannot export them."""
    if extension in FUSION_NATIVE:
        return '%s (%s)' % (native_reason(extension).rstrip('.'), detail[0].lower() + detail[1:])
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


def valid(document):
    """Whether a document object is still open in Fusion (isValid; missing counts as open, one that
    cannot be read as gone)."""
    try:
        return getattr(document, 'isValid', True) is not False
    except Exception:
        return False


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
