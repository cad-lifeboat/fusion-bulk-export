# Fusion Bulk Export

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
