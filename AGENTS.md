# Guidance for AI coding agents

You are helping a person back up **their own** Fusion account to **their own computer**
(macOS or Windows). This tool exports every design they can access as `.f3d` plus a
`.step` under its real name, in the hub/project/folder layout, and saves their other files
(PDFs, uploads) beside them. Drawings and electronics are not supported (exporting a
drawing through the API crashed Fusion): they are listed as such, never opened. Links
between designs are listed in the
manifest (`references`); Fusion's API cannot write `.f3z` bundles. It only reads from the
account. Work on the person's machine, in a terminal, with them nearby.

## Rules

- **Only the signed-in person's own account.** Do not run it for an account that is not
  theirs, and do not sign in for them.
- **Never upload, change or delete anything in the account.** The export only reads.
- **Names are private.** `manifest.json`, `manifest.journal`, `index.html`, `index.csv`,
  `inventory.json`, `export.log`, `autorun.log` and `autorun-result.json` contain hub,
  project and design names, and so does the output folder's own layout. Error text in
  those files can carry names too (the add-in's own `errors.log` leaves paths and names out,
  but check it before quoting it). Never paste any of it into chat transcripts, issues, pull requests,
  pastebins or anything that leaves the machine. Report counts, sizes and error *types*
  only; `scripts/autorun.py status` and `scripts/autorun.py check` (without `--names`)
  print counts only.
- Ask before deleting anything on disk, including a previous export folder.

## What needs the person

- Installing Fusion and signing in (their credentials, never yours).
- Once: switching the add-in on in Fusion. **Shift+S** (Utilities > Add-Ins > Scripts and
  Add-Ins), find **FusionBulkExport** in the list (Search, or tick **Add-ins** under
  **Type**), tick its **Run on Startup** box first, then switch **Run** on. The window
  closes when it is switched on; that is normal. Fusion keeps that setting itself; you
  cannot set it.
- Closing a Fusion dialog that blocks the export, or saving a design that is open with
  unsaved changes.
- Operating-system prompts (file access, firewall, the unsigned installer on macOS).

## Steps

Below, `python3` means `py` on Windows and `OUT` is the output folder.

1. **Prerequisites.** Fusion installed and signed in. Python 3.9+: `python3 --version`
   on macOS; on Windows `py --version` (else `winget install Python.Python.3.12`).
2. **Get the code.** `git clone` this repository, or download and unzip it. Work from
   its root folder.
3. **Pick OUT**, an absolute path: `~/FusionExport` on macOS, `C:\FusionExport` on
   Windows. Its length does not matter. Use the same OUT for every run.
4. **Inventory** (opens no design): `python3 scripts/autorun.py inventory OUT`. It prints
   hub, project, file and design counts; tell the person the counts only. If it says the
   add-in has not started, ask the person to switch it on (above); the command keeps
   waiting and carries on by itself. On a large account the inventory takes minutes.
5. **Check disk space.** Compare free space (`df -h ~`, or `Get-PSDrive C` on Windows)
   with the design count: budget roughly 5–20 MB per design, more for large assemblies.
   If it looks tight, tell the person before going further.
6. **Trial on one small project.** Read project ids and file counts from
   `OUT/inventory.json` locally (do not echo names) and pick a project with a few designs:
   `python3 scripts/autorun.py export OUT --project PROJECT_ID`.
   If one project holds nearly everything, add `--limit 3` instead: it stops after
   exporting 3 designs in this run (designs already up to date and other files do not
   count; the other files of the folders it visited, the rest of the last one included,
   are still saved); the full run reuses what it exported.
   The command prints one line saying which add-in it uses: it installs this checkout's
   only when none is installed or the installed one is older, never downgrades, and
   replaces nothing while an export runs. Do not work around that; tell the person if the
   line says an installed add-in is kept and they expected this checkout's.
7. **Check it:** `python3 scripts/autorun.py check OUT`. Expect exit 0: every file is there
   unchanged, every archive opens, every STEP has a valid header and ending. NOTE lines
   (a STEP missing bodies) are not failures.
8. **The rest.**
   - **Hundreds of designs or more: one project at a time.** Tell the person, then run
     `export OUT --project ID` per project into the same OUT, and `check OUT` after each.
     About 3–4 seconds per design in our small tests; complex designs take much longer,
     so time the first project.
   - Otherwise: `python3 scripts/autorun.py export OUT`.
   - Only part of the account: `--project ID` for whole projects, `--folder ID:Folder/Sub`
     or `--folder ID:id=FOLDER_ID` (ids from `folder_ids` in `inventory.json`) for single
     folders with their subfolders; both repeat. `--designs-only` leaves out the other files.
     Do not pass `--drawings`: drawing export was removed (it can crash Fusion), and the flag
     only prints a line saying so.
   - Progress: `python3 scripts/autorun.py status OUT` (counts only, including items still
     only in `manifest.journal`). Pause:
     `python3 scripts/autorun.py stop OUT`; resume by running the same export again.
   - Fusion cannot be used during an export and must stay signed in. The add-in keeps the
     computer awake.
9. **Finish:** run `export` once more to retry any failures, then `check OUT`, then tell
   the person the counts (exported, already up to date (designs), other files saved, other
   files already up to date, skipped, not supported, failed, total size, slow items, and any link notes from `status`), that `OUT/index.html` lists everything with reasons for any
   failure, and where OUT is.

## Troubleshooting

- **"Fusion has not started the add-in"**: ask the person to switch it on (above). The job
  waiting in `autorun.json` then starts; your command is still waiting and reports it.
- **"Fusion is busy"**: the person is exporting with the button. Only one export runs at a
  time; wait until their export has ended (its summary message appears), then run the same
  command again. The busy answer never touches the running export's folder.
- **"The add-in failed to start"** plus a traceback: the Run switch springs back off.
  Report the error type (not paths with the person's name in them) and stop.
- **"Stopped by itself"** in `status`: the code says why. `failing` means 5 designs in a
  row failed (offline or signed out?): check sign-in, then rerun. `disk_full`: free space
  or pick a folder on a bigger disk, then rerun with the same OUT. `output_missing` or
  `output_unwritable`: the folder went away or cannot be written.
- **A design failed because it is open with unsaved changes, or another version is
  open**: have the person save or close it in Fusion, then rerun the export.
- **Timeout or Ctrl-C**: if Fusion had not started the job yet, the command withdraws it
  and says so ("The job was not started by Fusion and has been withdrawn"); run the command
  again. If the job was running, the add-in may still be working: check `status`; rerun the
  same command to keep waiting. Raise `AUTORUN_TIMEOUT_SECONDS` for very large accounts.
- **"Another export is using this folder"**: `export.lock` in OUT belongs to a running
  export (another Fusion, or a job). Wait for it; do not delete the lock while it runs.
- **Folders that could not be read** (counted in `status` as `discovery_errors`): a
  project or folder the account cannot open (permissions, deleted, offline). They are
  skipped; report how many, not which.
- **Not supported** ("N not supported by Fusion's export (electronics, drawings)"):
  drawings, electronics and other Fusion-only types that Fusion's API cannot export (safely).
  They are neither failures nor skips; report the count, and tell the person they can export
  such files by hand in Fusion (File > Export) if they need them.
- **Skipped items**: other files left out with `--designs-only`, or a file Fusion would not
  hand out, with a reason. Report the count.
- **Slow items** (`status`: "Slow: N item(s)"): Fusion took over a minute on each (a design
  locked in another team can block it for minutes). Not a failure; report the count.
- **Fusion crashed or was closed mid-export**: restart Fusion and rerun the same command;
  the stale `export.lock` and `.staging` are cleared by themselves. Do not delete them by
  hand while an export may be running.
- **"Links: N design(s) failed with a linked design missing"** in `status`: those designs
  link to a design that was deleted, moved or cannot be read. The person fixes the links in
  Fusion, then reruns. **"N design(s) with out-of-date links"** is a note, not a failure.
  `inventory.json` has `has_references` and `references_out_of_date` per design.
  **"Links to other designs could not be read"** is a warning: Fusion raised on reading
  them, and the design was exported anyway; report the count.
- **Windows paths over 260 characters**: the result says how many items have one; they are
  saved, but Explorer may not open them. A shorter OUT (such as `C:\FusionExport`) avoids it.
  OUT itself may be at most 200 characters on Windows; a longer one is refused in one line.
- **Two hub folders in an old OUT**: resuming a folder from version 0.2 keeps the old
  `~hash` folders and puts new items in readable folders beside them. That is expected.
- **Warnings** (a STEP with no bodies, or fewer solids than the distinct bodies Fusion showed): the
  `.f3d` archive is still the complete copy; report the count.
