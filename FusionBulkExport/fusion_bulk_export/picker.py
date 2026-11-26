"""The command dialog's choice of what to export: everything, or a tree of hubs, projects and
folders shown in an HTML browser input (ui/tree.html) with tri-state checkboxes."""
import json
import os
from pathlib import Path

import adsk.core

from . import settings
from .core import OLDER_DELETE, OLDER_KEEP, OLDER_MOVE, Selection, collection, older_versions_choice
from .names import output_root_problem
from .words import plural

EVERYTHING = 'Everything'
CHOOSE = 'Choose projects or folders'
# Checkbox labels stay short (about 40 characters): Fusion puts a checkbox's label in the dialog's
# left column, sizes that column to its longest label and cuts the label off at its limit, which
# leaves the right-hand column (and anything in it) a sliver.
STEP = 'Also save a STEP file for each design'
STEP_WHY = ('A STEP file opens in almost any CAD program; the .f3d opens only in Fusion. For an assembly the '
            'STEP is the file that holds the geometry of all its linked parts.')
STEP_OFF_NOTE = 'Without STEP you get only .f3d files, which open only in Fusion.'
OTHER_FILES = 'Also save other files (PDFs, uploads)'
OTHER_FILES_WHY = 'PDFs, images and other uploaded files are copied as they are, beside the designs.'
# The CAM tool libraries (toollibs.py), saved once per export, at its start. The note under the box is
# always shown and, like STEP_OFF_NOTE, stays one short line.
TOOL_LIBRARIES = 'Tool libraries'
TOOL_LIBRARIES_NOTE = 'Your CAM tool libraries, one .json file each.'
TOOL_LIBRARIES_WHY = ('Your own tool libraries from Manufacture > Tool Library (local, cloud and hub ones; not '
                      "Autodesk's sample libraries) go into the Tool libraries folder, ready to import into Fusion "
                      'again. Nothing in Fusion is changed.')
# The .f3z built beside each assembly's .f3d at the end of the export (f3z.py), off by default. Its
# note is always shown and, like STEP_OFF_NOTE, stays one short line.
F3Z = 'Also build .f3z for assemblies'
F3Z_NOTE = 'A single file with its inserted designs, beside the .f3d.'
F3Z_WHY = ("An assembly's .f3d holds only links to the designs inserted into it. With this checked, an .f3z "
           '(Fusion Archive) is also built beside it from the .f3d files of this export: the assembly with every '
           'design it inserts, in one file. The .f3d files Fusion wrote are never changed; nothing in your Fusion '
           'account is touched.')
# What happens to a design's older versions once its newest is saved (core.OLDER_VERSIONS): a
# dropdown, Move first and the default. The note under it changes with the choice; like
# STEP_OFF_NOTE it stays one short line, since a longer one is cut off at the dialog's width.
OLDER = 'Older versions of a design'
OLDER_CHOICES = ((OLDER_MOVE, 'Move to the Older versions folder'), (OLDER_KEEP, 'Keep beside the newest'),
                 (OLDER_DELETE, 'Delete'))
OLDER_WHY = ('When a design got a new version since the last export, what happens to the files this export '
             'wrote for its older versions once the newest is saved and checked. Move (the default): they go '
             'into the "Older versions" folder at the top of the export folder, under the same folder path. '
             'Keep: they stay beside the newest. Delete: they are deleted, which cannot be undone. Only files '
             'this export wrote, and only if unchanged since; nothing in your Fusion account is touched.')
OLDER_NOTES = {OLDER_MOVE: 'Older versions move to the Older versions folder.',
               OLDER_KEEP: 'Older versions stay beside the newest.',
               OLDER_DELETE: 'Older versions are deleted once the newest is saved.'}
SAVE_TO = 'Save to'
BROWSE = 'Browse…'
OLD_FUSION = ('Choosing projects or folders needs a newer Fusion (update Fusion to get it). '
              'This one can export everything.')
# Dialog size in Fusion's units: wide enough for project names in the tree beside its boxes.
# 800 high: on a screen 1080 points high every control is in view at first (the tree is last),
# also the note shown while the STEP box is unchecked, the ones under the tool libraries and .f3z boxes
# and the one under the older-versions choice (each row about 30 units: the controls above the tree
# come to about 480, with the tree's 260 and the OK row below them).
DIALOG_SIZE = (520, 800)
DIALOG_MINIMUM = (380, 460)
# The tree's height in Fusion's units, set explicitly on the HTML input when it is made and again
# each time it is shown (on macOS, test builds showed the tree collapsed to a ~40-unit strip when
# nothing but the page's own content decided its height). The page (ui/tree.html) holds the same
# minimum in its CSS, so its content height never falls below it either.
TREE_MIN_HEIGHT = 260
TREE_MAX_HEIGHT = 0  # 0: no maximum, the tree grows with the dialog (given at creation only)
PATH_LIMIT = 64  # longer output folders are shown with their middle cut out (full path as tooltip)
TREE_HTML = Path(__file__).resolve().parent.parent / 'ui' / 'tree.html'


MANIFEST = Path(__file__).resolve().parent.parent / 'FusionBulkExport.manifest'


def version(manifest=MANIFEST):
    """The add-in's version from its manifest (what Fusion's add-in list shows), or None."""
    try:
        with open(str(manifest), encoding='utf-8') as stream:
            value = json.loads(stream.read()).get('version')
        return str(value) if value else None
    except (OSError, ValueError, AttributeError):
        return None


def shown_path(path, limit=PATH_LIMIT):
    """The output folder as the dialog shows it: whole, or with the middle cut out when long,
    so both the drive or home and the folder's own name stay readable."""
    path = path or ''
    if len(path) <= limit:
        return path
    tail = limit * 3 // 5
    return path[:limit - tail - 1] + '…' + path[-tail:]


def full_width(control, trace):
    """Across the whole dialog, not in its right-hand column. Inputs of an older Fusion may lack
    isFullWidth: the dialog then still opens, just narrower."""
    try:
        control.isFullWidth = True
    except Exception as error:
        trace('isFullWidth: %s' % error)


def size_tree(tree, trace):
    """Give the tree its explicit height limits (BrowserCommandInput.minimumHeight, and
    maximumHeight when there is one); an older Fusion without them keeps the ones given at
    creation. "No maximum" (0) is given only at creation: real Fusion takes 0 there but refuses
    it for the maximumHeight property ("3 : invalid argument value", logged twice per dialog)."""
    limits = [('minimumHeight', TREE_MIN_HEIGHT)] + ([('maximumHeight', TREE_MAX_HEIGHT)] if TREE_MAX_HEIGHT else [])
    for name, value in limits:
        try:
            setattr(tree, name, value)
        except Exception as error:
            trace('%s: %s' % (name, error))


def describe(control, tooltip, trace):
    try:
        control.tooltip = tooltip
    except Exception as error:
        trace('tooltip: %s' % error)


def folder_id(folder):
    identity = getattr(folder, 'id', None)
    return None if identity is None else str(identity)


class Picker:
    """Builds the dialog inputs and answers the tree page.

    The page asks for "hubs" (hubs with their projects, and how many items of each are already
    in the output folder), "folders" (the subfolders, with ids, and the file count of a folder
    in a project, listed only when the user opens it; addressed by folder ids where known, else
    by name path) and reports "selection" after every change, in the Selection format
    {project id: None | [folder paths]}, where each folder in a path is {"id", "name"} when its
    id is known (so a folder is told apart from a same-named sibling) and its name otherwise.
    """
    def __init__(self, app, inputs, trace=lambda message: None):
        self.app = app
        self.trace = trace
        self.projects = {}
        self.chosen = {}
        remembered = settings.load()
        number = version()
        # Top to bottom: version; Save to [Browse…] with the folder and its note; the checkboxes;
        # the Export choice; what is chosen; the tree, last, so that only tree rows can fall below
        # the dialog's first size and every control is in view (the tree scrolls inside itself).
        # Rows without a label span the whole dialog (isFullWidth as well, since the HTML tree
        # otherwise sits in the right-hand column).
        title = 'Fusion Bulk Export' + (' ' + number if number else '')
        self.version = inputs.addTextBoxCommandInput('fbx_version', '', title, 1, True)
        full_width(self.version, trace)
        try:
            self.version.formattedText = '<small><font color="#8c8c8c">%s</font></small>' % title
        except Exception as error:
            trace('formattedText: %s' % error)
        # Output folder: remembered, else the first-use suggestion (created when the export starts).
        self.output = remembered.get('output') or settings.default_output()
        # One row: the "Save to" label and the Browse… button (a button-style bool input shows its
        # name as the label and its text on the button).
        self.browse = inputs.addBoolValueInput('fbx_browse', SAVE_TO, False, '', False)
        try:
            self.browse.text = BROWSE
        except Exception as error:
            trace('button text: %s' % error)
        describe(self.browse, 'Choose the folder the export goes to.', trace)
        self.folder = inputs.addTextBoxCommandInput('fbx_folder', '', '', 1, True)
        full_width(self.folder, trace)
        self.note = inputs.addTextBoxCommandInput('fbx_note', '', '', 1, True)
        full_width(self.note, trace)
        self.facts = None
        self.browsing = False
        self.refresh_folder()
        # On unless the user switched it off last time; the note under it shows while it is unchecked.
        self.step_box = inputs.addBoolValueInput('fbx_step', STEP, True, '', remembered.get('step') is not False)
        describe(self.step_box, STEP_WHY, trace)
        self.step_note = inputs.addTextBoxCommandInput('fbx_step_note', '', STEP_OFF_NOTE, 1, True)
        full_width(self.step_note, trace)
        self.step_note.isVisible = not self.step_box.value
        # On unless the user switched it off last time.
        self.files = inputs.addBoolValueInput('fbx_files', OTHER_FILES, True, '',
                                              remembered.get('other_files') is not False)
        describe(self.files, OTHER_FILES_WHY, trace)
        # On unless the user switched it off last time.
        self.tools_box = inputs.addBoolValueInput('fbx_tools', TOOL_LIBRARIES, True, '',
                                                  remembered.get('tool_libraries') is not False)
        describe(self.tools_box, TOOL_LIBRARIES_WHY, trace)
        self.tools_note = inputs.addTextBoxCommandInput('fbx_tools_note', '', TOOL_LIBRARIES_NOTE, 1, True)
        full_width(self.tools_note, trace)
        # Off unless the user switched it on last time.
        self.f3z_box = inputs.addBoolValueInput('fbx_f3z', F3Z, True, '', remembered.get('f3z_bundles') is True)
        describe(self.f3z_box, F3Z_WHY, trace)
        self.f3z_note = inputs.addTextBoxCommandInput('fbx_f3z_note', '', F3Z_NOTE, 1, True)
        full_width(self.f3z_note, trace)
        # Move unless the user chose otherwise last time (an unknown remembered value is Move too);
        # the note under it says what the choice does.
        chosen = older_versions_choice(remembered.get('older_versions'))
        self.older = inputs.addDropDownCommandInput(
            'fbx_older', OLDER, adsk.core.DropDownStyles.TextListDropDownStyle)
        for value, name in OLDER_CHOICES:
            self.older.listItems.add(name, value == chosen, '')
        describe(self.older, OLDER_WHY, trace)
        self.older_note = inputs.addTextBoxCommandInput('fbx_older_note', '', OLDER_NOTES[chosen], 1, True)
        full_width(self.older_note, trace)
        self.mode = inputs.addDropDownCommandInput(
            'fbx_mode', 'Export', adsk.core.DropDownStyles.TextListDropDownStyle)
        self.mode.listItems.add(EVERYTHING, True, '')
        self.tip = inputs.addTextBoxCommandInput(
            'fbx_tip', '', 'Hundreds or thousands of designs? Start with one project or folder '
            '(Choose projects or folders), check it, then do the rest into the same output folder.', 3, True)
        full_width(self.tip, trace)
        self.tree = self.summary = None
        add_browser = getattr(inputs, 'addBrowserCommandInput', None)
        if add_browser is not None:
            self.summary = inputs.addTextBoxCommandInput('fbx_summary', '', 'Nothing chosen yet.', 1, True)
            full_width(self.summary, trace)
            self.summary.isVisible = False
        try:
            self.tree = (add_browser('fbx_tree', '', TREE_HTML.as_uri(), TREE_MIN_HEIGHT, TREE_MAX_HEIGHT)
                         if add_browser else None)
        except Exception as error:
            trace('no tree: %s' % error)
        if self.tree is None:
            # Older Fusion: no HTML in command dialogs, so no tree; everything still exports.
            old = inputs.addTextBoxCommandInput('fbx_old', '', OLD_FUSION, 2, True)
            full_width(old, trace)
            self.tip.isVisible = False  # it speaks of choosing, which this Fusion cannot offer
            self.summary = None
        else:
            full_width(self.tree, trace)
            size_tree(self.tree, trace)
            self.mode.listItems.add(CHOOSE, False, '')
            self.tree.isVisible = False
        if self.tree is not None and remembered.get('mode') == CHOOSE:
            for item in self.mode.listItems:
                item.isSelected = item.name == CHOOSE
            self.show_mode()
        trace('picker built')

    @property
    def other_files(self):
        """Whether files that are not designs are saved too (the checkbox)."""
        return bool(self.files.value)

    @property
    def tool_libraries(self):
        """Whether the CAM tool libraries are saved too (the checkbox)."""
        return bool(self.tools_box.value)

    @property
    def f3z_bundles(self):
        """Whether an .f3z is built beside each assembly too (the checkbox)."""
        return bool(self.f3z_box.value)

    @property
    def step(self):
        """Whether each design's STEP is saved too (the checkbox)."""
        return bool(self.step_box.value)

    @property
    def older_versions(self):
        """What happens to older versions' files once the newest is saved: 'move', 'keep' or
        'delete' (the dropdown; nothing chosen is 'move')."""
        item = self.older.selectedItem
        return next((value for value, name in OLDER_CHOICES if item is not None and name == item.name), OLDER_MOVE)

    @property
    def drawings(self):
        """Always False: drawings are never exported. Kept for an entry file loaded before an
        update, which still asks for it until Fusion restarts the add-in."""
        return False

    @property
    def choosing(self):
        return self.mode.selectedItem is not None and self.mode.selectedItem.name == CHOOSE

    def show_mode(self):
        self.tree.isVisible = self.summary.isVisible = self.choosing
        self.tip.isVisible = not self.choosing
        if self.choosing:
            size_tree(self.tree, self.trace)  # again once shown: its height never left to its content

    def refresh_folder(self):
        self.folder.text = shown_path(self.output)
        describe(self.folder, self.output or '', self.trace)
        self.facts = settings.folder_facts(self.output)
        self.note.text = settings.folder_note(self.facts) if self.output else 'Choose where to save the export.'
        problem = output_root_problem(self.output)
        if problem:
            self.note.text = problem  # Export stays grayed out (ready()) until another folder is chosen

    def choose_folder(self):
        """Browse…: the system folder dialog, starting at the current folder."""
        dialog = self.app.userInterface.createFolderDialog()
        dialog.title = 'Fusion Bulk Export output folder'
        try:
            start = self.output if self.output and os.path.isdir(self.output) else ''
            if start:
                dialog.initialDirectory = start
        except Exception as error:
            self.trace('initialDirectory: %s' % error)
        if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
            return False
        self.output = os.path.normpath(dialog.folder)  # Fusion returns C:/x on Windows; show C:\x
        self.refresh_folder()
        self.tell_page_exported()
        return True

    def changed(self, changed_input):
        if changed_input.id == self.browse.id:
            # A button-style input: each click is one inputChanged. Its value is not reset here,
            # since setting it could fire inputChanged again.
            if not self.browsing:
                self.browsing = True
                try:
                    self.choose_folder()
                finally:
                    self.browsing = False
        elif changed_input.id == self.step_box.id:
            self.step_note.isVisible = not self.step
        elif changed_input.id == self.older.id:
            self.older_note.text = OLDER_NOTES[self.older_versions]
        elif changed_input.id == self.mode.id and self.tree is not None:
            self.show_mode()
            self.trace('mode choosing=%s' % self.choosing)

    def ready(self):
        """Whether Export can be pressed: a folder is set and something is chosen."""
        return bool(self.output) and output_root_problem(self.output) is None and self.selection() is not None

    def remember(self):
        settings.store(dict(output=self.output, mode=self.mode.selectedItem.name if self.mode.selectedItem
                            else EVERYTHING, other_files=self.other_files, step=self.step,
                            older_versions=self.older_versions, tool_libraries=self.tool_libraries,
                            f3z_bundles=self.f3z_bundles),
                       drop=('drawings', 'linked_copies', 'keep_latest_only'))

    def incoming(self, action, data):
        """Answer the page; the returned string is handed back to its JavaScript. A request that
        fails answers {"error": "..."} so the page can show it with Retry."""
        request = json.loads(data) if data else {}
        self.trace('html %s' % action)
        try:
            if action == 'hubs':
                return json.dumps(dict(hubs=self.hubs()))
            if action == 'folders':
                folder = self.folder_at(str(request['project']), request.get('path') or [], request.get('ids') or [])
                children = [dict(id=folder_id(child), name=child.name) for child in collection(folder.dataFolders)]
                try:
                    files = int(folder.dataFiles.count)  # a count, without listing the files
                except Exception:
                    files = None
                return json.dumps(dict(folders=children, files=files))
        except Exception as error:
            self.trace('html %s failed: %s' % (action, error))
            return json.dumps(dict(error=str(error) or type(error).__name__))
        if action == 'selection':
            self.chosen = {str(key): value for key, value in request.items()}
            whole = sum(1 for value in self.chosen.values() if value is None)
            folders = sum(len(value) for value in self.chosen.values() if value is not None)
            self.summary.text = ('%s and %s chosen.' % (plural(whole, 'whole project'), plural(folders, 'folder'))
                                 if self.chosen else 'Nothing chosen yet.')
            return json.dumps(dict(ok=True))
        return ''

    def hubs(self):
        exported = (self.facts or {}).get('projects') or {}
        hubs = []
        for hub in collection(self.app.data.dataHubs):
            projects = []
            for project in collection(hub.dataProjects):
                self.projects[str(project.id)] = project
                projects.append(dict(id=str(project.id), name=project.name,
                                     exported=exported.get(str(project.id), 0)))
            hubs.append(dict(id=str(hub.id), name=hub.name, projects=projects))
        return hubs

    def folder_at(self, project, path, ids):
        """The folder at path in project; by id where the page knows it, else by name."""
        folder = self.projects[project].rootFolder
        for index, name in enumerate(path):
            identity = ids[index] if index < len(ids) else None
            children = list(collection(folder.dataFolders))
            match = None
            if identity is not None:
                match = next((child for child in children if folder_id(child) == str(identity)), None)
            if match is None:
                match = next((child for child in children if child.name == name), None)
            if match is None:
                raise LookupError('The folder "%s" is no longer there.' % name)
            folder = match
        return folder

    def tell_page_exported(self):
        """After the output folder changed: mark the projects already exported there."""
        if self.tree is None:
            return
        try:
            self.tree.sendInfoToHTML('exported', json.dumps((self.facts or {}).get('projects') or {}))
        except Exception as error:
            self.trace('sendInfoToHTML: %s' % error)

    def selection(self):
        """Selection for what is checked; None means nothing was chosen."""
        if not self.choosing:
            return Selection()
        return Selection(self.chosen) if self.chosen else None
