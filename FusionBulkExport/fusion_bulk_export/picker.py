"""The command dialog's choice of what to export: everything, or hubs, projects and folders."""
import adsk.core

from .core import Selection, collection

EVERYTHING = 'Everything'
CHOOSE = 'Choose projects or folders'


def folder_paths(folder, path=()):
    """Every folder below folder, as name paths, parents first. Lists folders only, no files."""
    for child in collection(folder.dataFolders):
        child_path = path + (child.name,)
        yield child_path, child
        yield from folder_paths(child, child_path)


class Picker:
    """Builds the dialog inputs and turns the ticked boxes into a Selection.

    Hubs and projects are listed when the dialog opens (hidden until "Choose" is picked);
    a project's folders are listed only when its "Pick folders" box is ticked, so large
    accounts open quickly.
    """
    def __init__(self, app, inputs, trace=lambda message: None):
        self.app = app
        self.inputs = inputs
        self.trace = trace
        self.boxes = {}  # input id -> (kind, hub, project, extra)
        self.inputs_by_id = {}  # our own references; nested inputs need no lookup
        self.groups = []
        self.folder_boxes = {}  # project input prefix -> [boxes]
        self.mode = inputs.addDropDownCommandInput(
            'fbx_mode', 'Export', adsk.core.DropDownStyles.TextListDropDownStyle)
        self.mode.listItems.add(EVERYTHING, True, '')
        self.mode.listItems.add(CHOOSE, False, '')
        for h, hub in enumerate(collection(app.data.dataHubs)):
            group = inputs.addGroupCommandInput('fbx_hub_%d' % h, hub.name)
            group.isVisible = False
            self.groups.append(group)
            every = group.children.addBoolValueInput('fbx_hub_%d_all' % h, 'All projects in this hub',
                                                     True, '', False)
            self.boxes[every.id] = ('hub', hub, None, [])
            self.inputs_by_id[every.id] = every
            for p, project in enumerate(collection(hub.dataProjects)):
                prefix = 'fbx_p_%d_%d' % (h, p)
                project_group = group.children.addGroupCommandInput(prefix, project.name)
                whole = project_group.children.addBoolValueInput(prefix + '_whole', 'Whole project',
                                                                 True, '', False)
                pick = project_group.children.addBoolValueInput(prefix + '_pick', 'Pick folders',
                                                                True, '', False)
                # Groups stay expanded: Fusion refuses to fold a group while the dialog is built.
                self.boxes[whole.id] = ('project', hub, project, pick)
                self.inputs_by_id[whole.id] = whole
                self.inputs_by_id[pick.id] = pick
                self.boxes[pick.id] = ('pick', hub, project, (prefix, project_group, whole))
                self.boxes[every.id][3].append(whole)

        trace('picker built: %d hub group(s), %d box(es)' % (len(self.groups), len(self.boxes)))

    @property
    def choosing(self):
        return self.mode.selectedItem is not None and self.mode.selectedItem.name == CHOOSE

    def changed(self, changed_input):
        """Handle an inputChanged event for one of our inputs."""
        if changed_input.id == self.mode.id:
            self.trace('mode %r choosing=%s groups=%d' % (
                getattr(self.mode.selectedItem, 'name', None), self.choosing, len(self.groups)))
            for group in self.groups:
                group.isVisible = self.choosing
                self.trace('group %s visible=%s' % (group.id, group.isVisible))
            return
        kind, hub, project, extra = self.boxes.get(changed_input.id, (None, None, None, None))
        if kind == 'hub':
            for whole in extra:
                whole.value = changed_input.value
        elif kind == 'project':
            # A whole project needs no folder choice.
            extra.isEnabled = not changed_input.value
            if changed_input.value and extra.value:
                extra.value = False
                self.show_folders(project, self.boxes[extra.id][3], False)
        elif kind == 'pick':
            self.show_folders(project, extra, changed_input.value)

    def show_folders(self, project, extra, visible):
        prefix, project_group, whole = extra
        boxes = self.folder_boxes.get(prefix)
        if boxes is None:
            if not visible:
                return
            boxes = []
            for index, (path, _) in enumerate(folder_paths(project.rootFolder)):
                label = '    ' * (len(path) - 1) + path[-1]
                box = project_group.children.addBoolValueInput('%s_f_%d' % (prefix, index), label, True, '', False)
                self.boxes[box.id] = ('folder', None, project, list(path))
                self.inputs_by_id[box.id] = box
                boxes.append(box)
            if not boxes:
                project_group.children.addTextBoxCommandInput(prefix + '_none', '', 'No folders in this project.',
                                                              1, True)
            self.folder_boxes[prefix] = boxes
        for box in boxes:
            box.isVisible = visible

    def selection(self):
        """Selection for the ticked boxes; None means nothing was chosen."""
        if not self.choosing:
            return Selection()
        chosen = {}
        for box_id, (kind, hub, project, extra) in self.boxes.items():
            box = self.inputs_by_id[box_id]
            if not box.value or not box.isVisible:
                continue
            if kind == 'project':
                chosen[str(project.id)] = None
            elif kind == 'folder' and chosen.get(str(project.id), []) is not None:
                chosen.setdefault(str(project.id), []).append(extra)
        # Folders ticked before their whole project was ticked are covered by the project.
        for box_id, (kind, hub, project, extra) in self.boxes.items():
            if kind == 'project' and self.inputs_by_id[box_id].value:
                chosen[str(project.id)] = None
        return Selection(chosen) if chosen else None
