# Guidance for AI coding agents

You are helping a person back up **their own** Fusion account to **their own computer**
(macOS or Windows). This tool exports every design they can access as `.f3z` (designs
with references) or `.f3d`, plus a `.step`, keeping the hub/project/folder layout. It
only reads from the account. Work on the person's machine, in a terminal, with them nearby.

## Rules

- **Only the signed-in person's own account.** Do not run it for an account that is not
  theirs, and do not sign in for them.
- **Never upload, change or delete anything in the account.** The export only reads.
  Do not use the self-test (`scripts/mac-selftest.sh`): it uploads files.
- **Design names are private.** `manifest.json`, `inventory.json`, `export.log`,
  `autorun.log` and `autorun-result.json` contain hub, project and design names. Never
  paste them into chat transcripts, issues, pull requests, pastebins or anything that
  leaves the machine. Report counts, sizes and error *types* only; use
  `scripts/autorun.py status` and `scripts/verify-export.py` (without `--names`), which
  print counts only.
- Ask before deleting anything on disk, including a previous export folder.

## What needs the person

- Installing Fusion and signing in (their credentials, never yours).
- Quitting Fusion before the first run if it is open, or closing a dialog that blocks it.
- Possibly one click: **Utilities → Add-Ins → Scripts and Add-Ins → FusionBulkExport → Run**
  (tick **Run on Startup**) if Fusion does not start the add-in by itself.
- Operating-system permission prompts (file access, firewall).

## Steps

1. **Prerequisites.** Fusion installed and signed in. Python 3.9+: `python3 --version`
   on macOS; on Windows `py --version` (else `winget install Python.Python.3.12`).
2. **Get the code.** `git clone` this repository, or download and unzip it. Work from
   its root folder.
3. **Pick an output folder** with a short absolute path (at most 100 characters):
   `~/FusionExport` on macOS, `C:\FusionExport` on Windows. Below, `python3` means
   `py` on Windows and `OUT` is that folder.
4. **Inventory** (opens nothing): `python3 scripts/autorun.py inventory OUT`. It prints
   hub, project, file and design counts. If it reports the add-in has not started, ask
   the person for the Add-Ins **Run** click above.
5. **Check disk space.** Compare free space (`df -h ~`, or `Get-PSDrive C` on Windows)
   with the design count; budget roughly 5–20 MB per design, more for large assemblies.
   If space looks tight, tell the person before going further.
6. **Trial on one small project.** Read project ids and file counts from
   `OUT/inventory.json` locally (do not echo names), pick a project with a few designs:
   `python3 scripts/autorun.py export OUT --project PROJECT_ID`.
7. **Verify:** `python3 scripts/verify-export.py OUT`. Expect exit 0: every archive
   opens, every STEP has a valid header and ending.
8. **Full export:** `python3 scripts/autorun.py export OUT`. Large accounts take hours;
   Fusion stays open and must stay signed in, and the computer must not sleep.
   - Progress: `python3 scripts/autorun.py status OUT`.
   - Pause: `python3 scripts/autorun.py stop OUT`. Resume by running the same export
     command again; finished designs are verified by hash and skipped.
9. **Finish:** run `verify-export.py OUT` again, rerun `export` once more to retry any
   failures, then tell the person the counts (found, done, failed, skipped, total size)
   and where the folder is.

## Troubleshooting

- **"Fusion has not started the add-in"**: ask for the Add-Ins **Run** click. The job
  waiting in `autorun.json` then starts.
- **"Fusion is open but the add-in is not watching"**: the person quits Fusion (closing
  any dialog first), or does the **Run** click; then rerun your command.
- **A design is "failed" with "Close this design in Fusion"**: it was open in Fusion.
  Have the person close it and rerun the export.
- **Timeout**: the add-in may still be working. Check `status`; rerun the same command
  to keep waiting. Raise `AUTORUN_TIMEOUT_SECONDS` for very large accounts.
- **Disk full**: stop, free space or choose another folder on a bigger disk, then rerun
  `export` with the same folder to resume.
- **"could not read" lines in `export.log`**: a project or folder the account cannot
  open (permissions, deleted, offline). They are skipped; report how many, not which.
- **STEP solid count differs from Fusion's body count** (`verify-export.py`): STEP
  dropped or merged bodies (e.g. surface bodies, or translator limits). The `.f3d`/`.f3z`
  archive is still the complete copy; note the count to the person.
- **Drawings and non-design files are "skipped"**: expected; only Fusion designs export.
