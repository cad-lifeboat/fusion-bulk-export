# inventory.json: the account listing (`cad-export-inventory/1`)

Status: draft for review. Describes `inventory.json` as written by Fusion Bulk Export from the
release after 0.3.5, for other programs that read it. A machine-readable JSON Schema (draft
2020-12) is in [`inventory-schema.json`](inventory-schema.json).

An inventory lists every hub, project and file the signed-in account can see, with their ids and
versions, **without opening anything** in Fusion. It is what a program reads to find the ids a
[job](job-protocol.md) needs (`designs`, `hubs`, `projects`, `folders`), and to compare an export
folder's `manifest.json` (see [manifest-schema.md](manifest-schema.md)) with the whole account.

It is written by:
- a protocol job with `action: "inventory"` ([job-protocol.md](job-protocol.md)): into the job's
  `output_dir`; the status file's `inventory` field names it once the job is `done`;
- `python3 scripts/autorun.py inventory OUT` (an `autorun.json` job with `"mode": "inventory"`):
  into `OUT`.

`inventory.json` holds hub, project, folder and file names. It stays on the person's computer;
a program reading it must not send it, or anything quoted from it, anywhere else.

## Identification and versioning

```json
{
  "schema_id": "cad-export-inventory/1",
  "hubs": {...},
  "unreadable": [...],
  "stopped": false,
  "counts": {...}
}
```

- `schema_id` (string, first key): the documented schema this file follows,
  `"cad-export-inventory/1"`. Written from the release after 0.3.5. **A file without it** (written
  by 0.3.5 or earlier) has the same layout: read it as `cad-export-inventory/1`.
- There is no integer `schema` and no `exporter` here: the program that wrote it is in
  `autorun-result.json` beside it, and the status file of the job names the add-in's version
  (`agent_version`).

Rules (as for `manifest.json`):
- **Additive changes keep `/1`**: a new optional field on a file, a project or a hub, a new key
  in `counts`. Readers must ignore fields they do not know.
- **Breaking changes go to `/2`**: removing or renaming a field documented as stable below,
  changing its type or meaning. A reader that sees a `schema_id` it does not know must stop and
  say so, not guess.
- Free text (error texts) is for people, not for parsing: it is Fusion's own wording and changes
  between Fusion versions.

## File format

- UTF-8, no byte-order mark, JSON (RFC 8259), indented by two spaces. Non-ASCII text is written
  as is (not `\u` escaped). Names are exactly as Fusion gives them.
- Written atomically: the full file goes to `inventory.json.tmp`, is flushed to disk, then renamed
  over `inventory.json`. A reader never sees a half-written file.
- **It is rewritten after each project** while the listing runs, so a long listing can be read
  before it ends. A file on disk is therefore not necessarily the finished listing: it is finished
  when the job that writes it is final (a protocol job's state `done`; for the command line,
  `autorun-result.json` beside it with `mode: "inventory"` and `finished` set). Until the first
  project is listed, the folder still holds the previous inventory, if any.
- Nothing in the account is opened, downloaded or changed: every value is a property of the
  listing Fusion hands out.

## Top level

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `schema_id` | string | yes | `"cad-export-inventory/1"` (may be absent in older files). |
| `hubs` | object | yes | Hub id → [hub](#hubs). |
| `unreadable` | array | yes | Places that could not be listed: `{"path": [names...], "error": text}`. `path` is `[hub name]` for a hub whose projects could not be listed, `[hub name, project name, folder names...]` for a project or folder. Those places are missing from `hubs` (a hub that could not be listed is present with no projects). |
| `stopped` | boolean | yes | `true`: the listing was cancelled (the job's `.cancel` file, or `STOP` in the folder) before every project was listed. It stops between projects: the projects listed are complete, the rest are absent. |
| `counts` | object | yes | See [counts](#counts). |

## hubs

`hubs` maps each **hub id** (Fusion's `DataHub.id`, an opaque string: use it as a job's `hubs`
value) to:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `name` | string | yes | The hub's name in Fusion. |
| `projects` | object | yes | Project id → [project](#projects). |

## projects

`projects` maps each **project id** (Fusion's `DataProject.id`, an opaque string: use it as a
job's `projects` value, and as `project` in its `folders`) to:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `name` | string | yes | The project's name in Fusion. |
| `files` | array | yes | Every file in the project, its root folder first, then each folder and its subfolders in turn: see [files](#files). |

Folders are not listed on their own: an empty folder does not appear, and a folder's id is found
in the `folder_ids` of the files below it.

## files

One object per file in the account (its current version only; older versions are not listed).

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `id` | string | yes | The file's cloud id (`DataFile.id`). For a design this is its **lineage URN** (`urn:adsk.wipprod:dm.lineage:...`): the same for all its versions, the same as `manifest.json` `entries[].id`, and what a job's `designs` list takes. |
| `name` | string | yes | The file's name as Fusion lists it (a design's without `.f3d`). |
| `path` | array of strings | yes | Folder names from the project root down to the file's folder (`[]` for the root). Names in Fusion, not names on disk. |
| `folder_ids` | array | yes | The id of each folder in `path`, in the same order (so the same length); `null` where Fusion gives a folder no id. The last one is the file's own folder: use it as `folder` in a job's `folders` (`{"project": project id, "folder": folder id}`) or with `--folder ID:id=FOLDER_ID` on the command line. `[]` for the root. |
| `extension` | string | yes | The file's extension as Fusion gives it, without the dot: `f3d` for a design, `f2d` for a drawing, `pdf`, ... Compare without regard to case. |
| `version` | integer | yes | The file's current version number in the account (`3` for v3). An export of that design records the same number in `manifest.json` `entries[].version` once it has exported this version. |
| `has_references` | boolean or null | yes | Designs only (`extension` `f3d`; absent for other files). `true`: Fusion says the design links to other designs (an assembly with inserted designs). `null`: Fusion does not say, or could not read it. |
| `references_out_of_date` | boolean or null | yes | Designs only. `true`: some of its links point at older versions of the linked designs (it is exported as it is, with a note). `null`: unknown. |
| `references_unreadable` | boolean | yes | Present (always `true`) only when Fusion raised on reading the two values above; both are then `null`. |
| `references_error` | string | yes (text free) | With `references_unreadable`: Fusion's error text. |

The links themselves (which designs a design links to) are not listed: that needs each design
read one by one. An export records them in `manifest.json` `entries[].references`, and a job
with a design list and `include_references` follows them by itself.

## counts

| Field | Meaning |
|---|---|
| `hubs` | Hubs in `hubs`. |
| `projects` | Projects listed, over all hubs. |
| `files` | Files listed, over all projects. |
| `designs` | Of those, files with extension `f3d` (any case). |
| `links_out_of_date` | Designs with `references_out_of_date: true`. |
| `links_unreadable` | Designs with `references_unreadable: true`. |
| `unreadable` | Items in `unreadable`. |

The same counts are in `autorun-result.json` `counts` and (as one line) in the job's status
`message`. All are stable.

## What a job's options change

An inventory job (or `autorun.py inventory`) lists everything the account holds, narrowed only by
`hubs` (whole hubs), `projects` (whole projects) and `folders`: with `folders`, the projects that
hold those folders are listed **whole**, not just the folders. A design list is refused for an
inventory.

## Reading an inventory: the common questions

| Question | Answer from inventory.json |
|---|---|
| Every design's id | `files[].id` of the files with `extension` `f3d`, over all hubs and projects. |
| Its current version | `files[].version`. |
| Is the export folder up to date with the account? | For each design, an entry in `manifest.json` with the same `id`, the same `version` and `status: "done"`. A higher `version` here means a newer version was saved since. |
| Which designs are assemblies? | `has_references: true` (`null`: unknown until exported). |
| Ids for a job of one folder | `{"project": <project id>, "folder": <last of folder_ids>}` of any file in that folder. |
| Was everything listed? | `stopped` is `false` and `unreadable` is empty, and the job is final. |

## What is not guaranteed

- The order of keys in `hubs` and `projects`, and the order of `files` beyond "root first, then
  each folder".
- The form of hub, project and folder ids: they are Fusion's, opaque, and may change between
  Fusion versions. Lineage URNs of designs are stable for the life of the design.
- Error texts.
