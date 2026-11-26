# Job protocol 1: asking the running add-in to export

Status: protocol 1, implemented by the add-in from the release after 0.3.5. Draft until that
release is out: details may still change.

Another program on the same computer, run by the same person, can ask Fusion Bulk Export (running
inside that person's Fusion) to export their designs into a folder, follow its progress, cancel
it, and read the result. Everything goes through files in one folder in the person's home. There
is no network port, no server and no account access beyond what the add-in already does.

The add-in still only reads from the Fusion account. A job can never make it upload, change or
delete anything in the account, or run anything but its own export.

It builds on the job file the add-in already takes from its command line (`autorun.json` beside
the add-in, results in `autorun-result.json`): same options, same validation, same export. What is
new is where the files live, that several jobs can queue, the progress file, canceling, and a
design list.

## Files

```
~/.cad-export/                      Windows: %USERPROFILE%\.cad-export\
  agents/
    <agent id>.json                 the running add-in announces itself (written by the add-in);
                                    this add-in's agent id is fusion-bulk-export
  jobs/
    <job id>.json                   a job (written by the other program, never changed by the add-in)
    <job id>.status.json            its state and progress (written by the add-in only)
    <job id>.cancel                 a request to cancel it (created by the other program)
```

- The add-in creates `~/.cad-export`, `agents` and `jobs` if missing, owner-only (mode 0700 on
  macOS). `~` is the home of the person running Fusion (`Path.home()`; on Windows
  `%USERPROFILE%`).
- All JSON is UTF-8 without a byte-order mark. Times are ISO 8601 with a UTC offset.
- **Every JSON file is written atomically, by both sides**: write the whole content to a temporary
  file in the same folder whose name starts with a dot (`.<job id>.json.<random>.tmp`), flush it,
  then rename it to its final name (`os.replace`, `MoveFileEx` with replace). Readers skip names
  starting with `.` and names ending in `.tmp`, so they never see a partial file.
- A job id is 1 to 64 characters from `A-Z a-z 0-9 . _ -`, starts with a letter or digit, and
  does not end in `.status` or `.cancel`. It must equal the job file's name without `.json`. A
  random UUID is the suggested form.

## Discovery: the agent file

While Fusion runs with the add-in switched on, the add-in keeps
`~/.cad-export/agents/<agent id>.json` (for this add-in `fusion-bulk-export.json`) up to date, and
removes it when the add-in stops (Fusion closing, or the person switching it off). A requester
reads every `*.json` in `agents/` and picks an agent by its `exporter` and `features`.

```json
{
  "protocol": 1,
  "protocols": [1],
  "agent": "fusion-bulk-export",
  "exporter": "fusion-bulk-export",
  "version": "0.3.5",
  "fusion_version": "2.0.21508",
  "fusion_running": true,
  "pid": 41237,
  "started": "2026-11-23T09:12:40.512000+00:00",
  "updated": "2026-11-23T09:30:11.004000+00:00",
  "busy": null,
  "job": null,
  "jobs_dir": "/Users/someone/.cad-export/jobs",
  "features": ["export", "inventory", "designs_by_id", "include_references", "hubs", "step", "step_only",
               "f3z_bundles", "tool_libraries", "older_versions", "cancel"]
}
```

| Field | Meaning |
|---|---|
| `protocol` | The protocol version this file is written in: `1`. |
| `protocols` | Job protocol versions the add-in accepts. |
| `agent` | Agent id; also the file name. One per person: Fusion runs once per user session. |
| `exporter`, `version` | The add-in and its version (as in `manifest.json`'s `exporter`). |
| `fusion_version` | Fusion's version string. |
| `fusion_running` | Always `true` while the file exists (kept for readers that expect it). |
| `pid` | Fusion's process id. |
| `started` | When the add-in started in this Fusion. |
| `updated` | Heartbeat, rewritten at most 10 s apart, also while a job runs (between its items; Fusion can hold it back while it opens or exports one large design). |
| `busy` | `null`, `"button"` (an export from the toolbar button is running) or `"job"` (a protocol job, or an `autorun.json` job from the command line). |
| `job` | Id of the protocol job running now, or `null` (also while an `autorun.json` job runs). |
| `jobs_dir` | Absolute path of the jobs folder. |
| `features` | What this version supports; a job may use only these (see [Versioning](#versioning-and-compatibility)). |

**Is the add-in there?**
- No file: not running (Fusion closed, or the add-in not switched on).
- `updated` less than 60 s old: running.
- `updated` older than 60 s: check `pid`. If that process is gone, the add-in is gone (Fusion
  crashed); if it is still running, Fusion is busy. The heartbeat comes from a background thread,
  and Fusion can hold it up for minutes while it opens one large design; so a stale heartbeat alone
  never means the add-in is gone. (The add-in's existing `autorun.alive` heartbeat and the
  command-line launcher use the same rule today.)

A job can be written while no add-in is running: it waits and is taken when Fusion next starts
with the add-in.

## Jobs

### The job file

Written once by the other program, atomically, as `~/.cad-export/jobs/<id>.json`. The add-in
never changes, renames or deletes it.

```json
{
  "protocol": 1,
  "id": "6f1c2a9e-3b4d-4e8f-9a21-0c5d7e3f1b2a",
  "requested": "2026-11-23T09:31:00+00:00",
  "requester": "Example importer 1.4",
  "action": "export",
  "output_dir": "/Users/someone/FusionExport",
  "designs": ["urn:adsk.wipprod:dm.lineage:AbCdEf123", "urn:adsk.wipprod:dm.lineage:GhIjKl456"],
  "include_references": true,
  "formats": ["f3d", "step"],
  "rebuilt_f3z": true,
  "older_versions": "move"
}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `protocol` | integer | required | `1`. |
| `id` | string | required | Equal to the file name without `.json`. |
| `requested` | string | required | When the job was written (ISO 8601). Jobs run in this order. |
| `requester` | string | `""` | Free text naming the requesting program, for the person's eyes (shown in the log). At most 200 characters. |
| `action` | string | `"export"` | `"export"`, or `"inventory"`: list every hub, project and design with ids and versions into `output_dir/inventory.json`, opening nothing (gives the ids a design list needs; its fields: [inventory-schema.md](inventory-schema.md)). |
| `output_dir` | string | required | Absolute path of the export folder. The same folder resumes an earlier export. |
| `designs` | `"all"` or array | `"all"` | `"all"`: everything the account holds, narrowed by `hubs`, `projects`, `folders`. Or a list of design ids (lineage URNs, as in `manifest.json` `entries[].id` and `inventory.json`): only those designs (their current version). |
| `include_references` | boolean | `true` | With a design list: also export every design those link to, and theirs, each once (so assemblies are complete and their `.f3z` can be built). Each linked design is exported at its current version, as an export of everything does; the version a link names is in `manifest.json` `references[].version`. Ignored with `"all"`. |
| `hubs` | array of strings | all | Hub ids. Only with `designs: "all"`. |
| `projects` | array of strings | all | Project ids. Only with `designs: "all"`. |
| `folders` | array | all | `{"project": project id, "folder": folder id}` each: that folder and its subfolders. Only with `designs: "all"`. |
| `formats` | array | `["f3d", "step"]` | `["f3d", "step"]` (both), `["f3d"]` (the archive only) or `["step"]` (shapes only: no `.f3d` is written; see [STEP only](#step-only)). Nothing else in protocol 1; no duplicates. |
| `rebuilt_f3z` | boolean | `false` | Build an `.f3z` beside each assembly's `.f3d` from the exported files, at the end. Needs `"f3d"` in `formats`. |
| `older_versions` | string | `"move"` | What happens to the files of a design's older versions in the folder once its newest is saved: `"move"` into `Older versions/`, `"keep"`, `"delete"` (cannot be undone; only when the person asked for it). |
| `tool_libraries` | boolean or `"only"` | `true` with `designs: "all"`, `false` with a design list | Also save the person's CAM tool libraries; `"only"`: only them (only with `"all"`). |
| `designs_only` | boolean | `false` | Leave out other files (PDFs, uploads). Implied by a design list. |
| `max_designs` | integer ≥ 1 | none | Stop after exporting this many designs (those already up to date do not count). |
| `memory_guard` | boolean | `true` | `false` switches off the stop when Fusion holds too much memory. |
| `hub_timeout_seconds` | number | `600` | How long to wait for the account's hubs after sign-in. |
| `x-*` | any | | Fields starting with `x-` are the requester's own; ignored. |

Any other field makes the job invalid (status `failed`, reason `invalid_job`, the message naming
the field), and so does a field of the wrong type or a combination that cannot work (`hubs`,
`projects` or `folders` with a design list, `rebuilt_f3z` without `"f3d"`, `tool_libraries:
"only"` with a design list, `action: "inventory"` with a design list). A requester checks the
agent file's `features` before using a field added later.

#### STEP only

`formats: ["step"]` writes each design's STEP file and its thumbnail, but no `.f3d`. Its
`manifest.json` entry says so honestly: `"archive": "off"`, and its `files` hold the `step` role
only (no `archive`). The design's links are still recorded in `references`, but their `path` is
`null` (they name `.f3d` files), and no `.f3z` can be built. If Fusion cannot write the STEP, the
design has nothing to show for it and is `failed`. A design exported earlier with its `.f3d`
counts as up to date (nothing is deleted); a design exported STEP-only is exported again by a
later job that asks for `"f3d"`. `python3 scripts/autorun.py check` checks a STEP-only entry's
STEP and counts it as "f3d not requested".

### Claiming, queuing and running

1. The add-in scans `jobs/` every few seconds (its existing watcher polls every 3 s) for
   `<id>.json` files that have no `<id>.status.json`.
2. It **claims** a job by creating `<id>.status.json` exclusively: it writes the status to a
   temporary file and hard-links it to the final name, which fails if the name exists (falling
   back to an exclusive create where hard links are not available). Only one add-in can claim a
   job, and the status file is never seen half-written. The job file itself is left untouched.
3. It validates the job at once. An invalid job, or one with an unknown `protocol`, is answered
   straight away with state `failed`.
4. Valid jobs are `queued` and run **one at a time**, in order of `requested` (then file time,
   then id). A job waits while an export from the toolbar button or from `autorun.json` runs;
   the status message says so. **`queued` may never be seen**: on an idle add-in a job is claimed
   as `queued` and set `running` in the same step, usually before the requester's next look. A
   requester must not wait for `queued`; it waits for any state, and then for a final one.
   A second job into the same folder as a queued or running one **queues behind it** (it is never
   refused for that): see step 5.
5. Before running, the add-in checks `output_dir/export.lock` (see [export.lock](#exportlock)).
   If another export holds it (another Fusion, the button into the same folder, the command line),
   the job **fails at once** with reason `folder_busy`; it is never run into a folder in use, and
   nothing in that folder is touched. (It never waits: the other export may run for hours. The requester submits a new job
   later.) Two jobs of this add-in into the same folder are not a clash: jobs run one at a time,
   so the second simply waits its turn in the queue and then carries on in that folder.
6. The job runs exactly as an `autorun.json` job does (same export code, same `manifest.json`,
   `index.html`, `autorun.log` and `autorun-result.json` in `output_dir`), while the add-in keeps
   `<id>.status.json` up to date.

### export.lock

Every export takes `export.lock` in its output folder for the whole run (from its first file to
the `.f3z` files at the end), whoever started it: the toolbar button, an `autorun.json` job from
the command line, a protocol export job, and `python3 scripts/autorun.py bundle` (which builds the
`.f3z` files without Fusion). An inventory never takes it. It is removed when the run ends, also
when it fails or is canceled; a crash leaves it behind.

```json
{"pid": 41237, "host": "someones-laptop.local", "started": 1795431063.512}
```

| Field | Meaning |
|---|---|
| `pid` | Process id of the export: Fusion's (or, for `bundle`, the Python running it). |
| `host` | The computer's name (Python's `socket.gethostname()`). |
| `started` | When the lock was taken, **seconds since 1970** (UTC, a number), not an ISO time. |

It is created with its contents in one step: written to a temporary file next to it, then
hard-linked to `export.lock`, which fails when a lock exists. Where a drive has no hard links (some
network or FAT drives), it is created exclusively (`O_CREAT | O_EXCL`) and then written, so there
it can be empty for an instant. It is one line of JSON, not indented.

**Stale rule.** The add-in, the command line and `bundle` all use the same rule (`lock.py`). A
lock is **stale** (left behind, taken over) when, in this order:
1. it cannot be read, or is not a JSON object: stale, unless the file was changed less than
   **10 seconds** ago (then it is an export creating it: held);
2. `started` is not a number, or is more than **12 hours** old: stale;
3. `host` is not this computer's name (another computer exporting into a shared folder): **held**
   (only the 12 hours make it stale);
4. `pid` is the reader's own process: stale unless that process holds it now (left by an earlier
   run in the same Fusion);
5. otherwise: stale when no process with `pid` runs on this computer, held while one does (on
   Windows, a process that cannot be asked counts as running).

An export that finds a held lock does not start; one that finds a stale lock deletes it and takes
its own. A recycled process id can make a lock look held until it is 12 hours old. A program
reading a folder (for [a complete export](manifest-schema.md#detecting-a-complete-export)) applies
the same rule.

**When a job gets `folder_busy`.** A protocol job (export or inventory) fails with `folder_busy`:
- when it starts (after its path is checked) and `output_dir/export.lock` exists
  and is held by the rule above: nothing in the folder is touched;
- or, rarely, when another export takes the lock between that check and the export's own start:
  the same reason, with `autorun.log` and `autorun-result.json` already written in the folder.

Never for another job of this add-in: jobs run one at a time, so a job into the same folder waits
in the queue (`queued`, or not yet claimed) and checks the lock only when its turn comes, after
the earlier job has released it. A job never waits on a held lock: it fails at once, because the
other export may run for hours. A command-line export into a held folder fails the same way
(`reason` `folder_busy` in `autorun-result.json`); the button says "Another export is using this
folder". An inventory from the command line does not check the lock.

### The status file

Written only by the add-in, atomically, after each item and at least every 10 s while Fusion lets
it, and at every change of state.

```json
{
  "protocol": 1,
  "id": "6f1c2a9e-3b4d-4e8f-9a21-0c5d7e3f1b2a",
  "agent": "fusion-bulk-export",
  "agent_version": "0.3.5",
  "agent_pid": 41237,
  "state": "running",
  "reason": null,
  "message": "Exporting design 12 of 40.",
  "claimed": "2026-11-23T09:31:02+00:00",
  "started": "2026-11-23T09:31:03+00:00",
  "updated": "2026-11-23T09:33:40+00:00",
  "finished": null,
  "progress": {"done": 11, "total": 40, "current": "urn:adsk.wipprod:dm.lineage:GhIjKl456", "current_number": 12},
  "counts": {"exported": 9, "up_to_date": 2, "files": 0, "files_up_to_date": 0, "skipped": 0, "unsupported": 0, "failed": 0},
  "output_dir": "/Users/someone/FusionExport",
  "manifest": null,
  "result": null,
  "exit_code": null
}
```

When finished:

```json
{
  "protocol": 1,
  "id": "6f1c2a9e-3b4d-4e8f-9a21-0c5d7e3f1b2a",
  "agent": "fusion-bulk-export",
  "agent_version": "0.3.5",
  "agent_pid": 41237,
  "state": "done",
  "reason": null,
  "message": "Exported 38 designs, 2 already up to date. Built 3 .f3z files.",
  "claimed": "2026-11-23T09:31:02+00:00",
  "started": "2026-11-23T09:31:03+00:00",
  "updated": "2026-11-23T09:36:12+00:00",
  "finished": "2026-11-23T09:36:12+00:00",
  "progress": {"done": 40, "total": 40, "current": null, "current_number": null},
  "counts": {"exported": 38, "up_to_date": 2, "files": 0, "files_up_to_date": 0, "skipped": 0, "unsupported": 0, "failed": 0},
  "output_dir": "/Users/someone/FusionExport",
  "manifest": "/Users/someone/FusionExport/manifest.json",
  "result": "/Users/someone/FusionExport/autorun-result.json",
  "exit_code": 0
}
```

| Field | Meaning |
|---|---|
| `state` | `queued`, `running`, `done`, `failed`, `cancelled`. The last three are final: the file does not change again. A job can go from claimed to `running` (or straight to `failed` when invalid) without `queued` ever being seen. |
| `reason` | `null` for `done`; else a code (below). |
| `message` | One line in plain words. Counts only: never a design, project or hub name. |
| `claimed`, `started`, `updated`, `finished` | Times; `started`/`finished` are `null` until then. |
| `progress.done` | Items finished in this job (designs and other files, whatever the outcome). |
| `progress.total` | Items the job will handle, when known: the design count for a design list (with the linked designs once found); for `"all"`, `null` until the whole account has been listed (the export works folder by folder as it lists). |
| `progress.current` | Id (lineage URN) of the item in progress, or `null`. |
| `progress.current_number` | Its 1-based number, or `null`. |
| `counts` | The run counts, as in `manifest.json` `runs[].counts` (`null` until the first item, and for an inventory). |
| `manifest` | Absolute path of `manifest.json` once the job is final (`done`, and `cancelled` or `failed` when an export was written); else `null`. Always `null` for an inventory. |
| `inventory` | For `action: "inventory"`: absolute path of `inventory.json` once the job is `done`; else `null`. Absent for an export. |
| `result` | Absolute path of the job's `autorun-result.json`, once written. |
| `exit_code` | What the command-line launcher would exit with: `0` done, `1` failed, `75` memory, `77` Fusion closed mid-job, `130` canceled. `null` until final. |

`done` means the export ran to its end. It can still hold failed items: check `counts.failed`
(and each entry's `status` in `manifest.json`). Running a new job with the same options retries
them; items already exported are checked and skipped.

Reason codes:

| `reason` | State | Meaning | What the requester does |
|---|---|---|---|
| `invalid_job` | failed | The job file is not valid; `message` says which field. | Fix and submit a new job. |
| `unsupported_protocol` | failed | `protocol` is not in the agent's `protocols`. | Use a protocol the agent lists, or update the add-in. |
| `folder_busy` | failed | Another export holds `output_dir/export.lock` (see [export.lock](#exportlock)). | Submit again later. |
| `output_unusable` | failed | Fusion cannot create or write `output_dir` (a network drive not connected in Fusion's session, no permission). | Use another folder. |
| `not_found` | failed | A hub, project, folder or design id the job names is not in the account. (A linked design that cannot be found is no failure: it is left out, and the assembly's link has `path: null`.) | Fix the ids (run an `inventory` job). |
| `not_signed_in` | failed | No hubs appeared within `hub_timeout_seconds`. | Ask the person to sign in to Fusion. |
| `failing` | failed | 5 designs in a row failed (offline, signed out). | Check sign-in; resubmit (it resumes). |
| `disk_full`, `output_missing`, `output_unwritable` | failed | The folder filled up, went away, or cannot be written. | Fix; resubmit. |
| `memory` | failed | Fusion itself held too much memory; stopped between designs. | Have Fusion restarted (the person, or close and reopen it when nothing is open in it); resubmit (it resumes). |
| `account_changed` | failed | A design changed in the account while it was exported. | Resubmit. |
| `interrupted` | failed | Fusion closed or crashed while the job ran (set when the add-in next starts). | Resubmit (it resumes). Repeated crashes on the same design: tell the person. |
| `error` | failed | Anything unexpected; `message` has the error type. | Report the type (not paths or names). |
| `cancelled` | cancelled | Canceled on request. | Nothing. |

### Canceling

The state and the reason code are spelled `cancelled` in protocol 1; messages say "canceled".

The requester creates an empty file `jobs/<id>.cancel`.
- A `queued` job becomes `cancelled` without running.
- A `running` job stops between items, as the `STOP` file in the output folder does today:
  everything finished so far is kept and recorded; state `cancelled`. A later job into the same
  folder carries on.
- A final job ignores it.

### Cleaning up

After a job is final the requester deletes `<id>.json`, `<id>.status.json` and `<id>.cancel`
(the requester owns the job; the add-in never deletes job files). Deleting a job file while its
job is `queued` withdraws it (it is not run; its status becomes `cancelled`); deleting it while `running` changes nothing (use
`.cancel`).

### Add-in restarts

When the add-in starts, it looks at status files that are not final and name an `agent_pid` whose
process is gone (or its own: the add-in was switched off and on again in the same Fusion): a
`running` one becomes `failed` with reason `interrupted` (`exit_code` 77); a `queued` one is taken
over (its `agent_pid` updated) and runs in turn. A status of another Fusion that is still running
is left alone.

## Mapping onto the existing `autorun.json` job

Protocol jobs are translated into the configuration `autorun.json` already takes and validated by
the same function (`autorun.check_settings`), then run by the same code (`autorun.run_autorun`
minus reading `autorun.json`).

| Protocol 1 field | `autorun.json` today | New? |
|---|---|---|
| `protocol`, `id`, `requested`, `requester` | none | New: checked, logged, kept in the status. |
| `action` | `mode` (`export`, `inventory`; `diagnostics` is not offered) | Same values. |
| `output_dir` | `output` | Renamed. |
| `designs: "all"` | no `projects`/`folders`: everything | Exists. |
| `projects` | `projects` | Exists. |
| `folders` (`{project, folder}`) | `folders: {project: [{"id": folder}]}` | Exists (by id). |
| `hubs` | none | **New**: a hub filter (`select_projects` by hub id). Small. |
| `designs: [lineage URN...]` | none | **New**: find each design by id (`Data.findFileById`; its `latestVersion` when Fusion hands out an older one), its folder path from `DataFile.parentFolder` up to the root, its project (`parentProject`) and hub (`parentHub`), and export exactly those files in that layout; with `include_references`, add the designs each links to (`childReferences`, recursively, each once). `progress.total` comes from this list. |
| `include_references` | none | **New** (with the design list). |
| `formats` | `step`, `archive` (true/false) | `"step"` in `formats` ⇔ `step: true`; `"f3d"` in `formats` ⇔ `archive: true` (**new** setting: `false` writes no `.f3d`). |
| `rebuilt_f3z` | `f3z_bundles` | Renamed. |
| `older_versions` | `older_versions` | Exists. |
| `tool_libraries` | `tool_libraries` | Exists; a design list defaults it to `false`. |
| `designs_only` | `designs_only` | Exists. |
| `max_designs` | `limit` | Renamed. |
| `memory_guard` | `memory_guard` | Exists. |
| `hub_timeout_seconds` | `hub_timeout_seconds` | Exists. |
| `.cancel` file | `STOP` in the output folder | **New** place; the export's `cancelled()` check looks at both. |
| status file | `autorun.log` lines, `autorun-result.json` at the end | **New** (progress callback already gives every value). |
| queue | one `autorun.json` at a time; refused with "busy" while the button export runs | **New**: queued instead of refused. |
| agent file | `autorun.alive` beside the add-in (time, busy) | **New** place and fields; `autorun.alive` stays for the launcher. |

`autorun.json` keeps working unchanged for the command-line launcher.

## Security

- The add-in reads jobs only from `~/.cad-export/jobs` of the user running Fusion, and writes only
  there, in `~/.cad-export/agents`, and inside each job's `output_dir`.
- It skips (and never claims) a job file that is a symbolic link, is not a regular file, is
  larger than 1 MB, or (macOS, Linux) is not owned by the user running Fusion. It creates its folders
  owner-only.
- A job can only ask for this add-in's own export or inventory. No field is ever run as a
  command, loaded as code or used as a URL; paths in a job are used only as the export folder.
- `output_dir` must be absolute; the existing checks on the export folder apply (on Windows at
  most 200 characters; a folder Fusion cannot use is refused).
- It never uploads, changes or deletes anything in the Fusion account. The only deleting a job
  can cause is `older_versions: "delete"` of files this export wrote earlier in `output_dir`.
- Unknown `protocol` versions and unknown fields are refused, not guessed.
- Status messages hold counts and error types, never names. `manifest.json` and the export hold
  names: they stay on the computer.
- Any program running as the same user can write a job. That is the same trust level as
  `autorun.json` today (anyone who can write the user's files can already run their programs).

## Installing the add-in

- Releases are on the project's GitHub releases page (tag `vX.Y.Z`). Each release has three
  assets:
  - `FusionBulkExport-X.Y.Z.zip`: the add-in (name unchanged).
  - `FusionBulkExport-X.Y.Z.zip.sha256`: one line in the usual `sha256sum` format,
    `<64 lowercase hex>  FusionBulkExport-X.Y.Z.zip`.
  - `FusionBulkExport-release.json` (the same name in every release):

    ```json
    {
      "version": "0.3.6",
      "zip_url": "https://github.com/cad-lifeboat/fusion-bulk-export/releases/download/v0.3.6/FusionBulkExport-0.3.6.zip",
      "sha256": "<64 lowercase hex of the zip>",
      "protocol": 1
    }
    ```

    `version` and `zip_url` name that release's zip, `sha256` is its SHA-256, `protocol` the
    highest job protocol it accepts. `min_fusion` (a Fusion version string), when present, is the
    oldest Fusion it supports; absent means no known minimum.
- A requester **pins** the version it installs: it downloads
  `https://github.com/cad-lifeboat/fusion-bulk-export/releases/download/vX.Y.Z/FusionBulkExport-X.Y.Z.zip`
  (and `.sha256`) for the version it was tested with. It uses
  `https://github.com/cad-lifeboat/fusion-bulk-export/releases/latest/download/FusionBulkExport-release.json`
  only to tell the person "a newer exporter is available"; it never installs that one by itself.
- Check the SHA-256 of the zip against the `.sha256` file (or `release.json`'s `sha256`) before
  using it.
- The zip holds `FusionBulkExport-X.Y.Z/FusionBulkExport/`: that folder is the add-in (with
  `FusionBulkExport.manifest` holding `version`, and `install-stamp.json` with the version and a
  hash of its files). Install it as `scripts/install.py` does: copy it to a temporary folder
  beside the target, remove the old `FusionBulkExport` folder, rename the new one into place.
  - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionBulkExport`
  - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionBulkExport`
- Never replace the add-in while it is exporting (agent file `busy` not `null`). Never install an
  older version over a newer one (compare `install-stamp.json` `version`). A replaced add-in takes
  effect when Fusion next starts.
- **The person must switch it on once in Fusion**: Shift+S (Utilities > Add-Ins > Scripts and
  Add-Ins), find FusionBulkExport, check **Run on Startup**, then switch **Run** on. No program can
  do this for them. Until they do, no agent file appears; a requester should say so in those
  words and keep waiting. Fusion keeps the setting when the folder is replaced by a newer version.

## Versioning and compatibility

- `protocol` is an integer. The add-in lists the versions it accepts in the agent file's
  `protocols`; a requester writes the highest version both know.
- Within protocol 1, changes are additive only: new optional job fields (announced in `features`
  first), new status fields, new `reason` codes, new `features`. A requester ignores status fields
  it does not know and treats an unknown `reason` as `error`.
- Anything else (a field removed or changing meaning, new required fields, a new layout of
  `~/.cad-export`) is protocol 2. The add-in keeps accepting protocol 1 for at least one minor
  version after adding 2.
- A job written for a newer protocol than the add-in knows fails with `unsupported_protocol`
  (written in the protocol-1 status format, which every version can read).
- The export folder's format is versioned separately: `manifest.json` `schema_id`
  (`cad-export-manifest/1`; see [manifest-schema.md](manifest-schema.md)), and an inventory's
  `inventory.json` `schema_id` (`cad-export-inventory/1`; see
  [inventory-schema.md](inventory-schema.md)).
