"""The command dialog's choice of what to export: everything, or a tree of hubs, projects and
folders shown in an HTML browser input (ui/tree.html) with tri-state checkboxes."""
import json
from pathlib import Path

import adsk.core

from .core import Selection, collection

EVERYTHING = 'Everything'
CHOOSE = 'Choose projects or folders'
OLD_FUSION = ('Choosing projects or folders needs a newer Fusion (update Fusion to get it). '
              'This one can export everything.')
TREE_HTML = Path(__file__).resolve().parent.parent / 'ui' / 'tree.html'


class Picker:
    """Builds the dialog inputs and answers the tree page.

    The page asks for "hubs" (hubs with their projects), "folders" (the subfolders at a path
    in a project, listed only when the user opens it) and reports "selection" after every
    tick, in the Selection format {project id: None | [folder paths]}.
    """
    def __init__(self, app, inputs, trace=lambda message: None):
        self.app = app
        self.trace = trace
        self.projects = {}
        self.chosen = {}
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
        trace('picker built')

    @property
    def choosing(self):
        return self.mode.selectedItem is not None and self.mode.selectedItem.name == CHOOSE

    def changed(self, changed_input):
        if changed_input.id == self.mode.id and self.tree is not None:
            self.tree.isVisible = self.summary.isVisible = self.choosing
            self.tip.isVisible = not self.choosing
            self.trace('mode choosing=%s' % self.choosing)

    def incoming(self, action, data):
        """Answer the page; the returned string is handed back to its JavaScript."""
        request = json.loads(data) if data else {}
        self.trace('html %s' % action)
        if action == 'hubs':
            hubs = []
            for hub in collection(self.app.data.dataHubs):
                projects = []
                for project in collection(hub.dataProjects):
                    self.projects[str(project.id)] = project
                    projects.append(dict(id=str(project.id), name=project.name))
                hubs.append(dict(id=str(hub.id), name=hub.name, projects=projects))
            return json.dumps(dict(hubs=hubs))
        if action == 'folders':
            folder = self.projects[str(request['project'])].rootFolder
            for name in request.get('path', []):
                folder = next(child for child in collection(folder.dataFolders) if child.name == name)
            return json.dumps(dict(folders=[child.name for child in collection(folder.dataFolders)]))
        if action == 'selection':
            self.chosen = {str(key): value for key, value in request.items()}
            whole = sum(1 for value in self.chosen.values() if value is None)
            folders = sum(len(value) for value in self.chosen.values() if value is not None)
            self.summary.text = ('%d whole project(s) and %d folder(s) chosen.' % (whole, folders)
                                 if self.chosen else 'Nothing chosen yet.')
            return json.dumps(dict(ok=True))
        return ''

    def selection(self):
        """Selection for what is ticked; None means nothing was chosen."""
        if not self.choosing:
            return Selection()
        return Selection(self.chosen) if self.chosen else None
