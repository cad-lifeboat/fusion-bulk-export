# Changelog

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
