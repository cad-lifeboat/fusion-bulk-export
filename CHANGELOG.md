# Changelog

## Unreleased

- Install steps match Fusion's current Scripts and Add-Ins window: no Add-Ins tab; tick Run on Startup before switching Run on.

## 0.3.0

Tested on real Fusion 2706.1.17 on macOS and Windows 11 before release: the dialog and tree, small and whole-account exports (59 designs), resume from a folder made by 0.2, cancel and continue, long output paths, the results dialog and the command line. Not yet exercised on real Fusion: runs of hundreds of designs with this version, the pause after repeated failures, and a design whose links Fusion cannot read (covered by automated tests only).

### What you get
- Readable names: files and folders keep their real names, in any language, with only the characters Windows or macOS cannot store replaced. Designs are saved as `<name> v<version>.f3d` and `.step`. A short `~1a2b` suffix is added only when two items in one folder would get the same name, and same-named sibling folders stay apart.
- Export folders made by earlier versions keep working: what is already there keeps its short name and is not exported again; new items go in readable folders beside them, and the report says so ("Older exports keep their old folder names; new items go in readable folders").
- Other files are saved too: PDFs, images and uploaded files as they are, under their own names.
- Drawings and electronics are **not supported**: exporting a drawing's PDF through Fusion's API crashed Fusion itself in testing (on Windows and macOS), so drawings are never opened. They are listed with "Fusion cannot export drawings safely through its API (use File > Export in Fusion)"; electronics and other Fusion-only types with the reason the API cannot export them. Both count as "N not supported by Fusion's export (electronics, drawings)": a neutral line of their own in the summary, `index.html`, `index.csv` (status `unsupported`) and `status`, apart from failures and from what you chose to skip.
- Linked designs: when a design with links fails, the reason reads "A linked design is missing or cannot be opened (N links)" instead of Fusion's raw error, which is kept beside it in the report. Designs whose links point to older versions are exported as they are and noted (not a failure) in the summary, `index.html`, `index.csv` and `status`. `inventory.json` marks designs with links and out-of-date links. When Fusion cannot read a design's links at all (it answers with an error), the design is still exported, with a warning: "Links to other designs could not be read; the design itself was exported". Before, such a design failed at once on both tries.
- On Windows, names are cut at 64 characters so typical layouts stay under 260-character paths; a file whose full path is still longer is saved, and the summary and `index.html` say how many there are (Explorer may not open them; a shorter output folder avoids it). The output folder itself may be at most 200 characters on Windows: a longer one is refused with that reason in the dialog and on the command line.
- Each design's thumbnail is saved beside it as `<name> v<version>.png`.
- Exported files carry the design's modified date.
- A report in the output folder: `index.html` lists every item with its status and links to its files, failures first with the reason and what to do; `index.csv` has the same as a table.
- Separate counts: exported now, already up to date (designs), other files saved, other files already up to date, skipped, not supported, failed. A re-run no longer looks as if nothing happened.
- Each STEP is checked as it is written: a broken file is tried again, and a STEP with no bodies, or fewer solids than the distinct bodies Fusion showed, is exported with a warning, shown at the end and in the report. Other differences in the count (a STEP may write a part used twice once or twice) are only recorded.
- Slow items: an item Fusion takes more than a minute on (a design locked in another team can block it for minutes) is listed under "Slow items" in `index.html` (`slow_seconds` in `index.csv`), and the summary says how many. A design whose failed attempt took more than two minutes is not tried a second time in that run.

### The dialog
- Shows the version that is running.
- **Save to** row with **Browse…**, remembered between runs (first suggestion: `Documents/FusionExport`). The line under it says when the folder already holds an export ("Continues the export in this folder").
- **Also save other files (PDFs, uploads)**, on by default and remembered.
- The tree: a filter box for project names, keyboard use (arrows, Space, Enter), a file count beside each opened project or folder, projects already in the chosen folder marked as exported, a loading row while a folder opens, and an error with **Retry** when a folder cannot be listed instead of a silent "No folders".
- The progress bar moves within a project, and the text shows the time so far, the average time per design and how many failed. Other files show as "file N". The item is named before Fusion starts on it, so a stall shows where (`export.log` gets an "exporting" line first too).
- The export dialog: the tree spans the whole width of the dialog and comes last, after **Save to**, the other-files box and the **Export** choice, so every control is in view at first (the dialog opens 520 x 600, which fits a screen 1080 points high); its height is set explicitly and no longer depends on what the page shows (a test build showed it as a thin strip on macOS); its scrollbars follow the light or dark theme. Long names end in "…" with the full name on hover; **Save to** and **Browse…** sit on one row, with the folder below (a very long path is shortened in the middle, the full path on hover).
- A results dialog at the end, with **Open report** and **Open folder** on its page and one **Close** button: a one-line headline (Finished, Cancelled, or Stopped with the reason), the counts with zero rows left out, up to three failures by name with "and N more in the report", "Run again to retry the N failed" when something failed, notes (not supported files, slow items, low disk space, long Windows paths) and the output folder. The page is written with the results already in it, so it never waits for Fusion to pass them on; if a button gets no answer within 2 seconds, the page shows the path with **Copy**. Only if Fusion cannot open the dialog at all does the same summary come in a message with OK, and `results-fallback.log` beside the add-in says why.
- Expected problems (a folder that cannot be used, a damaged manifest, a full disk, Fusion signed out) are explained in a sentence or two with what to do. Anything unexpected is saved to `errors.log` beside the add-in, and the dialog shows the details ready to copy into an issue. Those details leave out paths and project and design names.
- On a Fusion too old for the tree, the dialog still opens and offers Everything.

### Reliability on big accounts
- The export stops itself after 5 designs fail in a row (offline or signed out) or when the disk is full or the output folder is gone, instead of failing everything that is left. From the button it pauses with the reason and the last error and asks "Try again now?" (OK carries on, Cancel stops).
- One export at a time per output folder: `export.lock` is held while a run uses it, and a second export into the same folder (another Fusion, or a command-line job) is told "Another export is using this folder".
- Warns when less than 1 GB is free, and keeps the computer awake while it runs.
- Designs that an assembly opens alongside it are closed again afterwards, so a long run no longer fills Fusion with open documents. Designs you had open, and new unsaved documents, are never touched.
- After a crash of Fusion mid-export, the next run clears the stale `export.lock` (its process is gone) and the half-written `.staging` by itself.
- Finished items are recorded in a small journal file, and the manifest is rewritten only every 50 items or 30 seconds, instead of after every design.
- Resuming compares each file's size and modified time instead of reading every file again.
- No limit on how deep folders go (on Windows the output folder's own path may be at most 200 characters). Fusion writes to a short temporary path first and the add-in moves the file into place, because Fusion stops responding when handed a very long path on Windows.

### Command line
- `autorun.py check OUT` checks every exported file again without Fusion; `verify-export.py` now runs the same checks.
- `--designs-only` leaves out the other files. `--drawings` is still accepted for one release but does nothing: it prints one line saying drawing export was removed because it can crash Fusion.
- `--limit N` stops after exporting N designs in this run; designs already up to date and other files do not count, and the other files of every folder visited up to then (the rest of the folder where the limit was reached included) are still saved.
- `--folder PROJECT_ID:id=FOLDER_ID` chooses a folder by its id (listed in `inventory.json`), which also works for names with `/` in them.
- `status` prints the new counts, when a run stopped itself and why, low disk space, link problems and out-of-date links, and includes items still only in the journal.
- Stopping the command (Ctrl-C) or a timeout before Fusion started the job withdraws the job, and the command says so in one line (no traceback), so Fusion does not run it later by itself. An output folder that cannot be created is one plain line too.
- The toolbar button and command-line jobs work side by side: the button stays after a command-line export, and a job sent while someone exports with the button gets a "busy" answer instead of waiting. The answer is written beside the add-in, never into the running export's folder.
- The command installs the add-in from its checkout only when none is installed or the installed one is older by version: it never downgrades, keeps one of the same version, replaces nothing while an export runs, and says in one line which add-in it used.
- The command no longer edits the add-in's settings to make Fusion start it: if the add-in is not running, it says how to switch it on.
- `reset` is no longer needed; it only clears job files left by an earlier version.

### Install
- macOS: `Install (macOS).command` is not signed; the first time, right-click it and choose Open (or System Settings → Privacy & Security → Open Anyway). The README and the download's `README.txt` say so.
- The download is one folder, `FusionBulkExport-<version>`, with `Install (macOS).command`, `Install (Windows).bat` and a `README.txt`. The installers copy the add-in into Fusion's add-ins folder, so the green "+" step is no longer needed: switch it on in Fusion and tick Run on Startup.
- `scripts/install.py` does the same from a download of the repository (`--uninstall` removes it).
- Every installation carries `install-stamp.json` (its version and a hash of its files); `install.py`, both zip installers and the command line put the same one in place.

### Under the hood
- Removed a developer test from the add-in that could upload files: nothing in the download can upload, save or delete anything in your account, and a test checks the zip for it.
- Tests run on macOS, Windows and Linux for every change, with a privacy scan.
- `scripts/release.py prepare X.Y.Z` sets the version, moves these notes under it, runs the tests and builds the zip.
- `SECURITY.md` and `CONTRIBUTING.md`; the bug form asks for `errors.log`, which is safe to paste.

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
