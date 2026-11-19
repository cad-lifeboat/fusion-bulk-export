# Self-test on a Mac (for developers)

This uploads sample designs into the signed-in account, so use it only with a
throwaway account. To check your own export, use `scripts/verify-export.py`.

Use a throwaway account in a logged-in macOS desktop session. Install Fusion,
complete its first-launch setup and sign in once, then quit Fusion. Install Python 3
(`python3` on PATH). Put real `.f3d` and `.f3z` archives in a local seed directory;
include a linked assembly so reference recording is exercised. Seed subdirectories are
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
reference and solid-body counts, and caught errors with tracebacks, including retries.
A pass requires successful uploads, export of their returned file IDs, every design
exported as an `.f3d` that exists on disk, and its references recorded in the manifest. Even a recovered retry is
reported as a failed test so its error remains visible. Cached exports have no
fresh observation and cannot establish a passing test; use fresh output.
The check does not establish that the archive can be reimported or that STEP
preserves every design feature.

If configuration cannot be read or its output is unusable, diagnostics go to
`selftest-output` inside the installed add-in directory. A launcher timeout does
not cancel Fusion or an in-progress API call; inspect the log before retrying.
The polling timeouts cannot interrupt a synchronous Fusion API call.


Upload behavior follows Autodesk's [DataFolder.uploadFile documentation](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/core_DataFolder_uploadFile.htm)
and [DataFile.isComplete documentation](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/core_DataFile_isComplete.htm).
