"""The command dialog's choice of what to export: everything, or a tree of hubs, projects and
folders shown in an HTML browser input (ui/tree.html) with tri-state checkboxes."""
import json
import os
from pathlib import Path

import adsk.core

from . import settings
from .core import Selection, collection

EVERYTHING = 'Everything'
CHOOSE = 'Choose projects or folders'
OLD_FUSION = ('Choosing projects or folders needs a newer Fusion (update Fusion to get it). '
              'This one can export everything.')
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


def folder_id(folder):
    identity = getattr(folder, 'id', None)
    return None if identity is None else str(identity)


class Picker:
    """Builds the dialog inputs and answers the tree page.

    The page asks for "hubs" (hubs with their projects, and how many items of each are already
    in the output folder), "folders" (the subfolders, with ids, and the file count of a folder
    in a project, listed only when the user opens it; addressed by folder ids where known, else
    by name path) and reports "selection" after every tick, in the Selection format
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
        self.version = inputs.addTextBoxCommandInput(
            'fbx_version', '', 'Fusion Bulk Export' + (' ' + number if number else ''), 1, True)
        # Output folder: remembered, else the first-use suggestion (created when the export starts).
        self.output = remembered.get('output') or settings.default_output()
        self.folder = inputs.addTextBoxCommandInput('fbx_folder', 'Save to', self.output, 2, True)
        self.browse = inputs.addBoolValueInput('fbx_browse', 'Browse…', False, '', False)
        self.note = inputs.addTextBoxCommandInput('fbx_note', '', '', 1, True)
        self.facts = None
        self.browsing = False
        self.refresh_folder()
        self.mode = inputs.addDropDownCommandInput(
            'fbx_mode', 'Export', adsk.core.DropDownStyles.TextListDropDownStyle)
        self.mode.listItems.add(EVERYTHING, True, '')
        self.tip = inputs.addTextBoxCommandInput(
            'fbx_tip', '', 'Hundreds or thousands of designs? Start with one project or folder '
            '(Choose projects or folders), check it, then do the rest into the same output folder.', 3, True)
        self.tree = self.summary = None
        add_browser = getattr(inputs, 'addBrowserCommandInput', None)
        try:
            self.tree = add_browser('fbx_tree', '', TREE_HTML.as_uri(), 300, 0) if add_browser else None
        except Exception as error:
            trace('no tree: %s' % error)
        if self.tree is None:
            # Older Fusion: no HTML in command dialogs, so no tree; everything still exports.
            inputs.addTextBoxCommandInput('fbx_old', '', OLD_FUSION, 2, True)
            self.tip.isVisible = False  # it speaks of choosing, which this Fusion cannot offer
        else:
            self.mode.listItems.add(CHOOSE, False, '')
            self.tree.isVisible = False
            self.summary = inputs.addTextBoxCommandInput('fbx_summary', '', 'Nothing chosen yet.', 1, True)
            self.summary.isVisible = False
            if remembered.get('mode') == CHOOSE:
                for item in self.mode.listItems:
                    item.isSelected = item.name == CHOOSE
                self.show_mode()
        trace('picker built')

    @property
    def choosing(self):
        return self.mode.selectedItem is not None and self.mode.selectedItem.name == CHOOSE

    def show_mode(self):
        self.tree.isVisible = self.summary.isVisible = self.choosing
        self.tip.isVisible = not self.choosing

    def refresh_folder(self):
        self.folder.text = self.output or ''
        self.facts = settings.folder_facts(self.output)
        self.note.text = settings.folder_note(self.facts) if self.output else 'Choose where to save the export.'

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
        elif changed_input.id == self.mode.id and self.tree is not None:
            self.show_mode()
            self.trace('mode choosing=%s' % self.choosing)

    def ready(self):
        """Whether Export can be pressed: a folder is set and something is chosen."""
        return bool(self.output) and self.selection() is not None

    def remember(self):
        settings.store(dict(output=self.output, mode=self.mode.selectedItem.name if self.mode.selectedItem
                            else EVERYTHING))

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
            self.summary.text = ('%d whole project(s) and %d folder(s) chosen.' % (whole, folders)
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
        """Selection for what is ticked; None means nothing was chosen."""
        if not self.choosing:
            return Selection()
        return Selection(self.chosen) if self.chosen else None
