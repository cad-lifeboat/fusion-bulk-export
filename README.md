# Fusion Bulk Export

Get all of your Fusion designs out, in one go.

- Exports every design in every hub, project and folder you can access: `.f3z` when it references other designs (so linked parts come with it), otherwise `.f3d`, plus a matching `.step`.
- Keeps your project and folder layout on disk, writes a `manifest.json`, and can resume: run it again on the same folder and it skips what's done.
- Everything stays on your computer.

**Status: early.** It's tested against a simulated Fusion API but hasn't run on real Fusion yet. If you try it, please open an issue with what happened (your Fusion version and the end-of-run counts help).

## Install

In Fusion, open **Utilities → Add-Ins →
Scripts and Add-Ins**. On the **Add-Ins** tab, click the green **+**, select the included
`FusionBulkExport` folder, then select **FusionBulkExport** and click **Run**. Optionally enable
Run on Startup. This add-in is licensed under MIT; see LICENSE.

## Usage

Close designs before exporting. Choose **Fusion Bulk Export** in the Utilities /
Add-Ins panel and select a short output folder (at most 100 characters).
The add-in walks every accessible hub, project, and folder. The progress dialog
has a Cancel button; cancellation takes effect between items. An in-progress
Fusion API call must finish first. The final dialog shows counts and a selectable,
copyable output folder path.

Fusion must remain running and signed in. Retrieval and translation time depend on
design complexity, references, and network conditions. Already open designs are
reported as failed to preserve your editing session; close them and rerun.
Exported documents are closed without saving. No follow-up command is launched.

## Outputs

Each design produces `<output>/<hub>/<project>/<folders>/<name> v<version>.f3d`
and a matching `.step`. Designs with document or child references use `.f3z`
instead of `.f3d`. Only the current version exposed by each cloud file is exported;
previous versions are not enumerated. Drawings, other file types, and files without
a Design product are logged as skipped.

Stable identity suffixes distinguish names that collide after sanitizing, truncating,
or ignoring case. Reserved characters, reserved names, Unicode, and trailing spaces
or dots are normalized to portable ASCII. Names are shortened to keep absolute file
paths below 240 characters. Very deep folder paths become one `folders~<hash>`
directory; the complete original path remains in the manifest.

`manifest.json` contains original hub/project names, folder paths, names, cloud IDs,
version numbers, modified dates (Unix timestamps), statuses, errors, output paths,
SHA-256 hashes, byte counts, and run information. `export.log` is appended each run.
All generated output stays on your disk. Fusion retrieves cloud data through your
existing authenticated account. Manifests and logs contain design identifiers and
names, so treat them as private data.

## Check an export

```sh
python3 scripts/verify-export.py /path/to/output
```

checks every `done` entry: the archive is a readable zip (an `.f3z` holds its member
designs and `Manifest.json`), and the STEP has a valid header and ending and is not
trivially small. After a self-test it also compares the number of solids in each STEP
with the solid-body count Fusion reported. It prints counts only; add `--names` to
show design names.

## Resume

Choose the same output folder to resume. Completed pairs are skipped only if their
files, hashes, and sizes still match. Missing or changed files are regenerated.
Failed designs get at most two attempts per run. Cancelled runs preserve completed
work. New versions get separate filenames and manifest entries. Partial files can
remain after failures; only `done` entries represent complete archive/STEP pairs.

The manifest is replaced atomically after each item and failed attempt. An unreadable
manifest stops the run without overwriting it. Use only one export at a time per
output folder. Back up the directory as you would other valuable files.

Fusion version tested: **unknown until a volunteer run**. Synthetic tests do not
establish Fusion compatibility. A volunteer should verify referenced archive export
through `createFusionArchiveExportOptions` with `.f3z`, the reference properties
`document.documentReferences` and `dataFile.childReferences`, hidden document opening,
Design product lookup, STEP export, panel placement, and the progress/copy dialog.
STEP can omit design history and manufacturing information. Unavailable references,
permissions, or translator limitations are recorded as failures.

---

Not affiliated with or endorsed by Autodesk. Fusion is a trademark of Autodesk, Inc.

## Unattended Mac test

Use a throwaway account in a logged-in macOS desktop session. Install Fusion,
complete its first-launch setup and sign in once, then quit Fusion. Install Python 3
(`python3` on PATH). Put real `.f3d` and `.f3z` archives in a local seed directory;
include a linked assembly to exercise `.f3z` selection. Seed subdirectories are
searched recursively. No sample CAD data is bundled.

From this checkout, run:

```sh
./scripts/mac-selftest.sh /absolute/path/to/seeds /tmp/fusion-proof
```

The output directory must be new or empty, with an absolute path no longer than
100 characters. The optional third argument names the project (default
`bulk-export-selftest`). The script copies the add-in into
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionBulkExport`,
writes `selftest.json`, turns on `runOnStartup` in the installed copy for this one
launch, starts Fusion, waits up to one hour and prints a summary. On exit it turns
`runOnStartup` off again; the shipped manifest leaves it off.
It returns 0 for a passing test, 1 for reported failure, and a nonzero status for
launcher errors or timeout. Change the wait using `SELFTEST_TIMEOUT_SECONDS`.
Fusion is left running. Do not run multiple tests concurrently.

The default launch command is `open -a "Autodesk Fusion"`, using the app registered
with macOS LaunchServices. No fixed installation path is assumed. If your app is
named differently or is not registered, set its exact application bundle path:

```sh
FUSION_APP="/Applications/Autodesk Fusion.app" ./scripts/mac-selftest.sh /path/to/seeds /tmp/fusion-proof
```

Depending on your installation, the bundle may instead be under `~/Applications`
or have the older name `Autodesk Fusion 360.app`. The path above is an example;
use the installed `.app` on the test Mac. Fusion must start a fresh process so
`runOnStartup` executes. Login prompts, licensing screens and operating-system
permission prompts must have been handled during the initial setup.

When `selftest.json` exists beside the add-in entry point, startup runs the test
without creating command UI or displaying dialogs. Its minimal format is:

```json
{"output": "/tmp/fusion-proof", "seed_folder": "/absolute/path/to/seeds", "project": "bulk-export-selftest"}
```

The add-in renames the file to `selftest.json.last` as soon as it reads it, so the
test runs once and later launches start normally.

The test creates or reuses the named project in the first hub, uploads every seed
using `DataFolder.uploadFile`, polls `DataFileFuture.uploadState` and waits for
`DataFile.isComplete` before exporting. Each transfer and processing wait defaults
to 600 seconds; `upload_timeout_seconds` in the configuration changes this limit
(and the initial hub wait). Upload failure does not prevent trying the remaining
seeds or running the exporter. Nothing in any hub is deleted. Repeated tests
upload again and may add files or versions to the reused project.

By default the test exports only the self-test project (including designs created
when uploading assembly bundles), so nothing else in the account is opened. Set
`"scope": "all"` in the configuration to run the normal exporter over every
accessible hub and project instead.
`selftest.log` records activity immediately, including each upload and export
attempt, so a crash or stuck API call leaves a trail. `selftest-result.json` is
written atomically on completion and includes Fusion version, timestamps, elapsed
times, uploads, per-design manifest statuses and file hashes, reference counts,
archive selection checks, and caught errors with tracebacks, including retries.
A pass requires successful uploads, export of their returned file IDs, and every
design exported with its expected archive extension. Even a recovered retry is
reported as a failed test so its error remains visible. Cached exports have no
fresh archive observation and cannot establish a passing test; use fresh output.
The check establishes archive extension selection, not that the archive can be
reimported or that STEP preserves every design feature.

If configuration cannot be read or its output is unusable, diagnostics go to
`selftest-output` inside the installed add-in directory. A launcher timeout does
not cancel Fusion or an in-progress API call; inspect the log before retrying.
The polling timeouts cannot interrupt a synchronous Fusion API call.

The normal button, folder dialog and export behavior are unchanged.
Real Fusion compatibility remains unverified until this procedure passes on a Mac.

Upload behavior follows Autodesk's [DataFolder.uploadFile documentation](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/core_DataFolder_uploadFile.htm)
and [DataFile.isComplete documentation](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/core_DataFile_isComplete.htm).
