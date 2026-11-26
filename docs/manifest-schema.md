# manifest.json: the export folder's schema (`cad-export-manifest/1`)

Status: draft for review. Describes `manifest.json` as written by version 0.3.4 and later of
Fusion Bulk Export, for other programs that read an export folder. A machine-readable JSON Schema
(draft 2020-12) is in [`manifest-schema.json`](manifest-schema.json).

`manifest.json` holds hub, project and design names. It stays on the person's computer; a program
reading it must not send it, or anything quoted from it, anywhere else.

## Identification and versioning

```json
{
  "schema": 2,
  "schema_id": "cad-export-manifest/1",
  "exporter": {"name": "fusion-bulk-export", "version": "0.3.5"},
  "runs": [...],
  "entries": [...],
  "tool_libraries": [...]
}
```

- `schema` (integer, always present): the exporter's own format number. `2` today; folders from
  version 0.2 (`1`) are read and rewritten as `2` on the next run.
- `schema_id` (string): the documented schema this file follows. `"cad-export-manifest/1"`.
- `exporter` (object): `name` is always `"fusion-bulk-export"`; `version` is the add-in version
  (or command-line tool version) that last wrote the file.

`schema_id` and `exporter` are written on every save from the release after 0.3.5. A reader
must accept a file without them (older folders): `schema: 2` without `schema_id` reads as
`cad-export-manifest/1`.

Rules:
- **Additive changes keep `/1`**: a new optional field, a new value of `role`, a new key in
  `runs[]`. Readers must ignore fields they do not know, and treat an unknown `role` or
  `status` as "not for me" rather than as an error.
- **Breaking changes go to `/2`**: removing or renaming a field documented as stable below,
  changing its type or meaning, changing the path rules. The integer `schema` goes up at the same
  time. A reader that sees a `schema_id` it does not know (`cad-export-manifest/2`) must stop and
  say so, not guess.
- Fields marked **internal** below are the exporter's own bookkeeping. They are listed so a
  reader is not surprised by them, but they may change or go away in any version.

## File format

- UTF-8, no byte-order mark, JSON (RFC 8259), indented by two spaces. Non-ASCII text is written
  as is (not `\u` escaped). Names are exactly as Fusion gives them.
- Written atomically: the full file goes to `manifest.json.tmp`, is flushed to disk, then renamed
  over `manifest.json`. A reader never sees a half-written `manifest.json`.
- During a run `manifest.json` is rewritten every 50 finished items or 30 seconds and at the end.
  Items finished since the last rewrite are only in `manifest.journal` (see
  [Reading during a run](#reading-during-a-run)).
- Times: `runs[].started`, `runs[].finished`, `moved`, `removed`, `f3z.built`,
  `tool_libraries[].exported` are ISO 8601 with a UTC offset (`2026-11-23T10:15:02.123456+00:00`).
  An entry's `date` is different: see [entries](#entries).

## Path rules (guaranteed)

Every path in the manifest (`files[].path`, `thumbnail.path`, `f3z.path`, `moved_files[].moved_to`,
`references[].path`, `tool_libraries[].path`, ...):
- is **relative to the export folder** (the folder holding `manifest.json`), never absolute;
- uses **forward slashes** (`/`) on every operating system;
- **never contains a `..` segment** and never starts with `/` or a drive letter;
- names a file inside the export folder (the exporter refuses to treat any other path as its own).

These hold for everything the exporter writes. A manifest can still be edited by hand, so a reader
must reject any path that breaks them rather than follow it.

Within one folder the exporter never gives two items names that differ only in letter case or
Unicode normalization (macOS and Windows treat those as the same file). A path may be longer than
260 characters (`long_path: true` on its entry); on Windows open it with the `\\?\` prefix.

The top of the export folder holds the hub folders, plus these reserved names that are never a
hub: `Older versions/`, `Tool libraries/`, `manifest.json`, `manifest.journal`, `index.html`,
`index.csv`, `inventory.json`, `export.log`, `autorun.log`, `autorun-result.json`,
`export.lock`, `.staging/`, `STOP`.

## Top level

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `schema` | integer | yes | `2` (see above). |
| `schema_id` | string | yes | `"cad-export-manifest/1"` (new; may be absent). |
| `exporter` | object | yes | `{"name", "version"}` (new; may be absent). |
| `entries` | array | yes | One object per item (a version of a design, or another file). |
| `tool_libraries` | array | yes | CAM tool libraries saved; absent if never exported. |
| `runs` | array | partly | One object per run into this folder, oldest first. |
| `older_layout` | boolean | yes | `true`: the folder was started by version 0.2, and its old entries keep shortened folder names with a `~hash`. |

## entries

One object per item the export has dealt with. The same design appears once per **version** it
exported (`v3`, then `v4`); older versions stay in the list with status `done` (kept beside),
`moved` or `removed`.

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `key` | string | yes | Unique within the manifest. A JSON-encoded array `[hub id, project id, item id, version]`. Treat it as opaque. |
| `kind` | string | yes | `"design"` (a Fusion design, `.f3d`) or `"file"` (anything else: PDF, image, drawing, electronics, upload). Entries from version 0.2 may lack it: read as `"design"`. |
| `id` | string | yes | The item's cloud id. For a design this is its **lineage URN** (`urn:adsk.wipprod:dm.lineage:...`): the same for all its versions. |
| `version` | integer | yes | The version number exported (`3` for v3). |
| `name` | string | yes | The item's name in Fusion. |
| `hub` | string | yes | Hub name. |
| `project` | string | yes | Project name. |
| `path` | array of strings | yes | Folder names from the project root down to the item's folder (`[]` for the root). Names in Fusion, not the cleaned names on disk; use `files[].path` for the disk location. |
| `date` | integer | yes | When the version was saved in Fusion: Fusion's `dateModified`, **seconds since 1970-01-01 UTC**. Readers should tolerate a non-integer (an unreadable date is kept as Fusion gave it). |
| `status` | string | yes | See [status values](#status-values). |
| `error` | string or null | yes | Why the item is `failed`, `skipped` or `unsupported`; `null` when `done`. |
| `files` | array | yes | Files written for this item: see [files](#files). Empty unless `status` is `done` (or partly, `moved`/`removed`). |
| `thumbnail` | object | yes | The design's thumbnail, same shape as a `files[]` item with role `thumbnail`. Optional. |
| `step` | string | yes | Absent when the STEP was written. `"off"`: the run did not ask for STEP. `"failed"`: Fusion could not write it (`step_error` says why); the `.f3d` is still there. |
| `archive` | string | yes | Absent when the `.f3d` was written. `"off"`: the run asked for STEP only (a job's `formats: ["step"]`): `files` hold the STEP and no `archive`. (New; never `"off"` together with `step`.) |
| `references` | array or null | yes | Designs this design links to: see [references](#references). `null`: they could not be read. Absent: not recorded (other files; very old entries). |
| `has_references` | boolean or null | yes | Fusion says the design has links. `null`: unknown. |
| `references_out_of_date` | boolean or null | yes | Fusion says some links point at older versions of the linked designs. |
| `references_unreadable` | boolean | yes | `true`: Fusion raised on reading the links; `references_error` has its text. The design was exported anyway. |
| `f3z` | object | yes | The `.f3z` bundle built beside this assembly: see [f3z](#f3z-bundles). |
| `f3z_not_built` | string | yes | Why no `.f3z` was built for this assembly (plain words, no names). |
| `f3z_older` | array | yes | Earlier `.f3z` files replaced by a newer one: see [f3z](#f3z-bundles). |
| `moved` / `moved_files` | string / array | yes | For status `moved`: when, and where each file went. See [older versions](#older-versions). |
| `removed` / `removed_files` | string / array | yes | For status `removed`: when, and what was deleted. |
| `warnings` | array of strings | yes | Plain-language warnings (STEP with fewer solids than expected...). |
| `step_note` | string | yes | A note that is not a warning (`No solid bodies (sketches only): its STEP is empty.`). |
| `fusion_error` | string | yes | Fusion's own error text beside a plain-language `error`. |
| `long_path` | boolean | yes | A file's full path is over 260 characters. |
| `slow_seconds` | integer | yes | Fusion took this long (over a minute) on the item. |
| `kept` | string | yes | Why an older version's files were not moved or deleted. |
| `up_axis` | string or null | yes | Which way is up in the design: `"z"` (Z-up) or `"y"` (Y-up); `null`: unknown, with `up_axis_error`. See [up axis](#up-axis). Optional (new): absent in entries written before it existed and in other files; read absent as unknown. |
| `up_axis_error` | string | yes | Why `up_axis` is `null` (plain words, no names). Present only then. |
| `move_error`, `remove_error` | string | yes | Why moving or deleting an older version's files failed (retried next run). |
| `extension` | string | internal | File extension of an other file. |
| `attempts`, `base`, `location`, `identities`, `shortened`, `solid_bodies`, `visible_solid_bodies`, `visible_solid_bodies_distinct`, `surface_bodies`, `mesh_bodies`, `step_solids`, `step_includes_hidden`, `step_hidden_error`, `step_error`, `count_error`, `documents_left_open`, `linked_copy`, `linked_occurrences`, `links_broken`, `account_changed` | various | internal | Naming, checking and testing bookkeeping. |

### status values

| `status` | Meaning | Files on disk |
|---|---|---|
| `done` | Exported and checked. **Also used for an item found up to date on a later run**: "up to date" is a run count, never a status. | `files` (and `thumbnail`, `f3z`) are in the folder with the recorded size and SHA-256. |
| `failed` | Tried (twice) and did not work; `error` says why. The next run tries again. | None recorded. |
| `skipped` | Deliberately not saved: other files when only designs were asked for, a design without a design product, a file Fusion would not hand out. `error` says why. | None. |
| `unsupported` | A type an add-in cannot export: drawings, electronics. Neither failure nor skip. | None. |
| `moved` | An older version of a design whose files were moved into `Older versions/` once the newest version was saved. | `moved_files[].moved_to`. `files` holds any file that could not be moved. |
| `removed` | An older version whose files were deleted (the person chose Delete). | None (`removed_files` lists what was). |

There is no `verified` status in the file. A reader may see `verified` only in run counts
(`up_to_date`).

### up axis

A Fusion design is Y-up or Z-up, depending on the modeling-orientation preference in force when it
was created; the exported `.f3d` and `.step` keep the design's own coordinates. Fusion's API has no
documented property of a design for this, and the preference only applies to new designs, so it is
never used. Instead, when the design is opened for export, its own standard named views are read
(nothing in the design or the viewport is changed):

- the Top view's view direction (camera `target` minus `eye`) is normalized; about -Z means Z-up,
  about -Y means Y-up;
- the Front view's camera `upVector` is normalized; about +Z means Z-up, about +Y means Y-up;
- "about" means that component of the unit vector is at least 0.9.

`up_axis` is `"z"` or `"y"` only when both views give the same answer. When a view is missing, a
vector is along neither axis, the two disagree, or Fusion raises on reading them, `up_axis` is
`null` and `up_axis_error` says why. `null` (or an absent field) means unknown, never "Z".

It is recorded when the design is exported (with or without its `.f3d`). A design found up to date
on a later run is not opened again, so its entry keeps what it had; entries written before this
field existed have none until that design is exported again (a new version, or a missing file).

## files

Each item of `files` (and `thumbnail`, and `f3z` with extra fields) is:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `path` | string | yes | Relative path (see [path rules](#path-rules-guaranteed)). |
| `role` | string | yes | What the file is (below). Entries from before roles existed lack it: read `.f3d` as `archive`, `.step`/`.stp` as `step`, `.png` as `thumbnail`, anything else as `file`. |
| `bytes` | integer | yes | Size. |
| `sha256` | string | yes | Lowercase hex SHA-256 of the file's bytes. |
| `mtime_ns` | integer | internal | Modified time when recorded (the exporter's fast check on resume). |

`role` values:

| `role` | Where | Meaning |
|---|---|---|
| `archive` | design `files` | The design's `.f3d` as Fusion wrote it. Exactly one per `done` design, unless `archive` is `"off"` (none). |
| `step` | design `files` | The design's `.step` (all solid bodies, linked parts included). One per `done` design unless `step` is `"off"` or `"failed"`; always one when `archive` is `"off"`. |
| `thumbnail` | `thumbnail` | The design's `.png` thumbnail. |
| `file` | other-file `files` | The other file, as Fusion hands it out. Exactly one per `done` other file. |
| `f3z_bundle` | `f3z` | The `.f3z` built beside an assembly. |
| `archive_with_linked_parts` | design `files` | Testing only (an experimental switch, off by default). Ignore. |

## references

On a design entry, `references` lists the designs it links to (inserted components, derives), as
Fusion's `DataFile.childReferences` lists them:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `id` | string | yes | The linked design's lineage URN (matches that design's entry `id`). |
| `name` | string | yes | The linked design's name. |
| `version` | integer | yes | The version of it this design uses. Absent when Fusion did not give it (folders exported before 0.3.5 recorded none until their next run). |
| `path` | string or null | yes | Relative path of the linked design's **newest exported** `.f3d` in this folder (role `archive` of its newest `done` entry); `null` when it is not exported here. Recomputed at the end of every run. Never a path in `Older versions/`. |

One item per linked design, as Fusion lists them; how many times a design is inserted is not
recorded (Fusion's list gives each linked design once, as far as observed). The link's
`neutronRole` (the GUID Fusion gives each link inside the assembly) is not in `manifest.json`; it
is only inside a built `.f3z`'s `DesignDescription.json`, and only when the Python that built it
could read it (`f3z.roles: true`).

Note: `references[].path` is the newest exported version, which may differ from
`references[].version`. A reader that needs exactly the version used should look for the entry
with that `id` and `version` (status `done`, or `moved` with its `moved_files`).

## f3z bundles

Present only when the export was run with `.f3z` bundles on (or the `bundle` command was run).

`entry.f3z`: `role` (`"f3z_bundle"`), `path`, `bytes`, `sha256` as for files, plus:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `built` | string | yes | When it was built (ISO 8601). |
| `members` | array | yes | Designs inside: `{name, id, version, sha256, pinned}`; the assembly first. `sha256` is that of the `.f3d` copied in. `pinned`: the version is the one the link names (else the newest exported was taken). |
| `roles` | boolean | yes | `true`: the links' neutronRole is in its `DesignDescription.json`. |
| `format` | integer | internal | Build format; another value makes the exporter build it again. |
| `mtime_ns` | integer | internal | As for files. |

`entry.f3z_older[]`: earlier bundles of this assembly that a new one replaced: `path` (where it
was), `sha256`, `bytes`, `built`, `date` (when it was replaced), and one of `moved_to`, `kept_as`
(both relative paths) or `removed` (a date).

## older versions

When the person chose Move (the default), once a design's newest version is saved, the files this
export wrote for its older versions go to `Older versions/<same relative path>`. That entry then
has `status: "moved"`, `moved` (ISO date) and `moved_files[]`: `{path, moved_to, role, bytes,
sha256}` where `path` is where the file was and `moved_to` where it is now. With Delete:
`status: "removed"`, `removed` and `removed_files[]`: `{path, role, bytes}`. With Keep the older
entries stay `done` beside the newest.

## tool_libraries

One object per CAM tool library ever exported into the folder:

| Field | Type | Stable | Meaning |
|---|---|---|---|
| `key` | string | yes | Opaque id (a hash of the library's URL; the URL itself is not kept). |
| `kind` | string | yes | `"tool_library"`. |
| `location` | string | yes | `Local`, `Cloud`, `Hub` or `External`. |
| `folder` | array of strings | yes | Library folders below the location. |
| `name` | string | yes | Library name. |
| `path` | string | yes | Relative path of its `.json` (under `Tool libraries/`). |
| `tools` | integer or null | yes | Number of tools. |
| `bytes`, `sha256` | | yes | As for files. |
| `exported` | string | yes | When the file was written. |
| `status` | string | yes | `done` or `failed`. |
| `error` | string or null | yes | Why it failed. |
| `older` | array | yes | Previous copies: `path`, `sha256`, `bytes`, `exported`, `date`, and `moved_to`, `kept_as`, `removed` or `left` (why it stayed). |
| `url_scheme`, `seen`, `mtime_ns` | | internal | Bookkeeping. |

## runs

One object per run, appended at its start. Stable fields:

| Field | Type | Meaning |
|---|---|---|
| `started` | string | ISO 8601. |
| `finished` | string or null | `null` while the run is going, or when it ended without a final save (Fusion closed). |
| `canceled` | boolean | Stopped on request (Cancel, `STOP` file, a canceled job). New in versions after 0.4.2; absent in older runs (read `cancelled`). |
| `cancelled` | boolean | The same value under its older name, the only one up to 0.4.2; still written for readers of `cad-export-manifest/1`. Read `canceled` when present. |
| `stopped_code` | string or null | Why it stopped by itself: `failing` (5 designs in a row failed), `disk_full`, `output_missing`, `output_unwritable`, `memory`, `account_changed`. `null`: it did not. |
| `stopped_reason` | string or null | The same in plain words. |
| `error` | string | Present when the run ended with an unexpected error. |
| `counts` | object | `exported`, `up_to_date`, `files`, `files_up_to_date`, `skipped`, `unsupported`, `failed` (and internal `linked_copies`). Present once the run ended. |
| `found` | integer | Items found in the run. |
| `selection` | string | What was asked for (`everything`, or a description). |
| `step`, `other_files`, `f3z_bundles` | boolean | The run's options. |
| `archive` | boolean | The run wrote `.f3d` files (`false`: STEP only). New; absent in older runs (read as `true`). |
| `older_versions` | string | `move`, `keep` or `delete`. |
| `limit_reached` | boolean | The run stopped at its design limit. |
| `discovery_errors` | array | Folders that could not be read: `{path, error}`. |

Everything else in `runs[]` (`memory`, `gc`, `free_bytes`, `low_space`, `low_memory`,
`journal_replayed`, `moved`, `removed`, `f3z`, `tool_libraries`, `slow`, `documents_*`, ...) is
internal.

## Reading during a run

While an export runs, the folder has `export.lock` and the latest items may be only in
`manifest.journal`: one JSON object per line, `{"entry": {...}}`, each a complete entry. To get the
current state, read `manifest.json`, then for each journal line replace the entry with the same
`key` (or append it). Ignore a last line that does not parse (cut off by a crash).

`export.lock` is `{"pid", "host", "started"}` (`started` in seconds since 1970). It belongs to a
running export unless it is stale: unreadable, older than 12 hours, or (written on this computer)
its process is gone. The exact rule is in [job-protocol.md, export.lock](job-protocol.md#exportlock).

## Detecting a complete export

An export folder is complete and settled when all of these hold:
1. There is no `export.lock`, or it is stale by the rule in
   [job-protocol.md, export.lock](job-protocol.md#exportlock).
2. `manifest.journal` is absent or empty.
3. The last item of `runs` has `finished` set, `stopped_code: null`, no `error`,
   `limit_reached` absent, and `canceled: false` (`cancelled: false` in runs up to 0.4.2).
4. For the newest version of every design (`kind: "design"`), `status` is `done`; no entry the
   reader cares about has `status: "failed"`.

This says the export covered everything **it was asked for and found**. Items never visited
(another project, a run with a limit) have no entry at all; compare with `inventory.json`
([inventory-schema.md](inventory-schema.md)) to check against the whole account. `python3 scripts/autorun.py check <folder>` re-checks every
file's size and SHA-256 and that each archive and STEP opens.

## Reading an export: the common questions

How the usual questions of another program map onto the fields above.

| Question | Answer from manifest.json |
|---|---|
| Which designs are in the export? | Entries with `kind == "design"` and `status == "done"`; for each `id`, the one with the highest `version` (older versions kept beside are `done` too). |
| A design's id | `entry.id`, its lineage URN. |
| Its version and name | `entry.version`, `entry.name`. |
| Its `.f3d` | `entry.files[]` item with `role == "archive"`: `path`. None when `entry.archive` is `"off"` (STEP only). |
| Its STEP | `entry.files[]` item with `role == "step"`; none when `entry.step` is `"off"` or `"failed"`. |
| Its rebuilt `.f3z` | `entry.f3z.path` (assemblies only, when bundles were asked for); `entry.f3z_not_built` says why there is none. |
| Part or assembly? | Assembly when `entry.references` is a non-empty list; part otherwise. `has_references: true` with `references` null or empty means links exist but could not be listed. |
| What it links to | `entry.references[]`: `id`, `version`, `name`, `path` (or `null`). |
| How often each link is used | Not recorded: one item per linked design. |
| The link's role (neutronRole) | Not in `manifest.json`; only in the `.f3z`'s `DesignDescription.json` when `f3z.roles` is true. |
| Top-level assemblies | Assemblies whose `id` is in no other newest design's `references`. |
| Which failed, and why | Entries with `status == "failed"`: `error` (plain words), `fusion_error` (Fusion's text). |
| Not exportable | `status == "unsupported"` (drawings, electronics), with `error`. |
| Integrity | `bytes` and `sha256` on every file record. |
| Which way is up? | `entry.up_axis`: `"z"` or `"y"`; `null` or absent means unknown (`up_axis_error` says why). |
| Who wrote it | `exporter.name`, `exporter.version` (new); the folder's runs in `runs[]`. |
| When | `runs[-1].started` / `finished`; each design's own date in `entry.date` (seconds since 1970, UTC). |

All paths are relative to the folder holding `manifest.json`, with forward slashes and no `..`.

## What is not guaranteed

- The order of `entries`. (It is roughly the order items were exported in, but a resumed or
  replayed run changes it.)
- Folder and file names on disk: they are the item's real names, with characters the file
  system cannot store replaced and a `~1a2b` suffix where two items would collide; they can
  change between versions of the exporter for new items. Always take paths from the manifest.
- Anything marked internal.
