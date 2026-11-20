# Changelog

## Unreleased

**Not yet tested on real Fusion.** Everything below passes the automated tests against a simulated Fusion; 0.3.0 is released only after a test pass on real Fusion on macOS and Windows.

### What you get
- Readable names: files and folders keep their real names, in any language, with only the characters Windows or macOS cannot store replaced. Designs are saved as `<name> v<version>.f3d` and `.step`. A short `~1a2b` suffix is added only when two items in one folder would get the same name, and same-named sibling folders stay apart.
- Export folders made by earlier versions keep working: what is already there keeps its short name and is not exported again; new designs get readable names.
- Other files are saved too: PDFs, images and uploaded files as they are, under their own names, and drawings as PDF. Electronics and other Fusion-only file types are skipped with the reason.
- Each design's thumbnail is saved beside it as `<name> v<version>.png`.
- Exported files carry the design's modified date.
- A report in the output folder: `index.html` lists every item with its status and links to its files, failures first with the reason and what to do; `index.csv` has the same as a table.
- Separate counts: exported now, already up to date, other files saved, skipped, failed. A re-run no longer looks as if nothing happened.
- Each STEP is checked as it is written: a broken file is tried again, and a STEP with no bodies or fewer solids than Fusion showed is exported with a warning, shown at the end and in the report. Solid counts now count every visible instance of a part.

### The dialog
- Shows the version that is running.
- **Save to** row with **Browse…**, remembered between runs (first suggestion: `Documents/FusionExport`). The line under it says when the folder already holds an export ("Continues the export in this folder").
- **Also save other files (PDFs, drawings, uploads)**, on by default and remembered.
- The tree: a filter box for project names, keyboard use (arrows, Space, Enter), a file count beside each opened project or folder, projects already in the chosen folder marked as exported, a loading row while a folder opens, and an error with **Retry** when a folder cannot be listed instead of a silent "No folders".
- The progress bar moves within a project, and the text shows the time so far, the average time per design and how many failed.
- The last dialog shows the counts, the first failures and warnings, and opens the report or the folder.
- Expected problems (a folder that cannot be used, a damaged manifest, a full disk, Fusion signed out) are explained in a sentence or two with what to do. Anything unexpected is saved to `errors.log` beside the add-in, and the dialog shows the details ready to copy into an issue.
- On a Fusion too old for the tree, the dialog still opens and offers Everything.

### Reliability on big accounts
- The export stops itself after 5 designs fail in a row (offline or signed out) or when the disk is full or the output folder is gone, instead of failing everything that is left. From the button it asks whether to retry or stop.
- Warns when less than 1 GB is free, and keeps the computer awake while it runs.
- Designs that an assembly opens alongside it are closed again afterwards, so a long run no longer fills Fusion with open documents. Designs you had open are never touched.
- Finished items are recorded in a small journal file, and the manifest is rewritten only every 50 items or 30 seconds, instead of after every design.
- Resuming compares each file's size and modified time instead of reading every file again.
- No limit on how deep folders go or how long the output folder's path is. Fusion writes to a short temporary path first and the add-in moves the file into place, because Fusion stops responding when handed a very long path on Windows.

### Command line
- `autorun.py check OUT` checks every exported file again without Fusion; `verify-export.py` now runs the same checks.
- `--designs-only` leaves out the other files.
- `--folder PROJECT_ID:id=FOLDER_ID` chooses a folder by its id (listed in `inventory.json`), which also works for names with `/` in them.
- `status` prints the new counts, when a run stopped itself and why, and low disk space.
- The toolbar button and command-line jobs work side by side: the button stays after a command-line export, and a job sent while someone exports with the button gets a "busy" answer instead of waiting.
- The command no longer edits the add-in's settings to make Fusion start it: if the add-in is not running, it says how to switch it on.
- `reset` is no longer needed; it only clears job files left by an earlier version.

### Install
- The download is one folder, `FusionBulkExport-<version>`, with `Install (macOS).command`, `Install (Windows).bat` and a `README.txt`. The installers copy the add-in into Fusion's add-ins folder, so the green "+" step is no longer needed: switch it on in Fusion and tick Run on Startup.
- `scripts/install.py` does the same from a download of the repository (`--uninstall` removes it).

### Under the hood
- Removed a developer test from the add-in that could upload files: nothing in the download can upload, save or delete anything in your account, and a test checks the zip for it.
- Tests run on macOS, Windows and Linux for every change, with a privacy scan.
- `scripts/release.py prepare X.Y.Z` sets the version, moves these notes under it, runs the tests and builds the zip.
- `SECURITY.md` and `CONTRIBUTING.md`; the bug form asks for `errors.log`.

## 0.2.3

- Exports stream: each folder is listed and its designs exported before moving on, so the first files appear within seconds, even on large accounts (no long "Discovering designs…" wait).
- The progress dialog shows the project number, folder and design it is on, including while it is listing.
- README: a copy-paste prompt for AI agents, a note that timing varies with design complexity.

## 0.2.2

- Clearer quick start: download, add to Fusion, export, step by step.
- Big accounts: the README, AGENTS.md and the dialog itself suggest exporting one project or folder at a time into the same output folder, with timing to expect.
- Windows: the final dialog shows the output folder with backslashes.

## 0.2.1

- Large accounts: the inventory logs progress per project, saves `inventory.json` as it goes and stops between projects when a `STOP` file appears; `--limit` stops listing as soon as it has enough designs.
- Tested on Windows 11: inventory of a large account and a small trial export.

## 0.2.0

- Tested on real Fusion through the toolbar button (Everything, 59/59) and the launcher (one folder); the Export button stays greyed out until something is chosen; the dialog opens wide enough for names, and the button sits on the toolbar.
- Choose what to export in a tree of hubs, projects and folders (folders load as you open them; tri-state ticks; a project's own files count only when the project itself is ticked); `--project` and `--folder` do the same for the unattended launcher.
- Download the add-in as one zip from the release page; README starts with a three-step quick start.
- First real-Fusion run (2706.1.17, macOS): 59 of 59 designs exported and checked.
- A design already open in Fusion is exported from the open copy (unless it has unsaved changes or is another version).
- The manifest records each design's solid-body counts; `verify-export.py` compares them with the STEP (differences are notes; `--strict` makes them failures).
- Archives are always `.f3d`: Fusion's API cannot write `.f3z` (asked for one, it silently writes `.f3d`). Each design's referenced designs are listed in the manifest under `references`, and an export that reports success without writing its file is now a failure.
- Fixed: the add-in failed to load inside Fusion (its own folder is not on Python's path).
- `autorun.py diagnostics` records the export-related API of the running Fusion build.
- Unattended export on macOS and Windows (`scripts/autorun.py`): inventory, per-project trial, resumable full run, status, stop; the add-in keeps watching for new jobs in the same Fusion session.
- AGENTS.md: step-by-step guidance for an AI coding agent running the backup for you.
- Projects or folders that cannot be read are logged and skipped instead of ending the run.
- `scripts/verify-export.py` checks archives and STEP files in an export folder.

## 0.1.0

- First release: export every design in your hubs as .f3z (when it references other designs) or .f3d, plus a STEP of the same version; keeps the project and folder tree; resumable with a manifest.
