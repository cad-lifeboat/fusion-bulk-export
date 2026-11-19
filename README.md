# Fusion Bulk Export

![Fusion Bulk Export: every design in your Fusion account, saved to your own disk](docs/hero.png)

Get all of your Fusion designs out, in one go.

- Saves every design in every hub, project and folder you can access as a Fusion archive (`.f3d`) plus a `.step`, in the same project and folder layout.
- Resumable: run it again on the same folder and it skips what's already done.
- Everything stays on your computer. Nothing in your account is changed.

**Tested on real Fusion** (2706.1.17, macOS) with a real account: 59 of 59 designs exported, every archive and STEP file checked, no failures (3.5 minutes, 52 MB). Windows support is written but not yet tested on a Windows machine; if you try it there, please [open an issue](../../issues/new/choose) with your Fusion version and the end-of-run counts.

## Quick start

1. **Download** `FusionBulkExport-<version>.zip` from the [latest release](../../releases/latest) and unzip it.
2. **Add it to Fusion:** **Utilities → Add-Ins** (or press **Shift+S**) → **Add-Ins** tab → green **+** → choose the unzipped `FusionBulkExport` folder → select **FusionBulkExport** → **Run**.
3. **Export:** on the **Utilities** tab, click **Fusion Bulk Export** in the **Add-Ins** panel, choose an empty folder with a short path (such as `Documents/FusionExport`), and wait. A progress bar shows each design and has a Cancel button; the last dialog shows the counts.

Leave Fusion open and signed in while it runs, and keep the computer awake. To pick up where you left off, or to catch designs added later, run it again on the same folder.

**Let your AI do it.** If you use an AI coding agent, ask it to follow [AGENTS.md](AGENTS.md). It runs the whole backup unattended (inventory, a trial project, the full export and a check of every file) and only needs you for signing in and one click in Fusion.

## What you get

```
FusionExport/
  <hub>/<project>/<folders>/<design> v<version>.f3d    the full design: history, sketches, everything
  <hub>/<project>/<folders>/<design> v<version>.step   geometry for any other CAD program
  manifest.json                                        every design, its status and checksums
  export.log
```

- **`.f3d`** opens in Fusion with **Upload** or **File → Open → Open from my computer**.
- **`.step`** holds the visible bodies, without history. Most CAD programs open it.
- Drawings and other non-design files are listed as skipped.
- Only each design's current version is exported, not its older versions.
- **Linked designs:** Fusion's API can only save a design on its own, not as a `.f3z` bundle with the designs it links to. Every linked design is exported in its own right, so nothing is lost, and `manifest.json` lists each design's links under `references`. Re-linking after import is manual. If you need one self-contained `.f3z`, use **File → Export** in Fusion for that design.
- Names are made safe for every operating system and kept short. A short `~hash` suffix keeps similar names apart, and the original names are in the manifest.

`manifest.json` and the logs contain your project and design names. Keep them private, and don't attach them to issues.

## If something goes wrong

- **A design shows as failed:** run the export again on the same folder. Failed designs are retried, and finished ones are checked and skipped.
- **"Open in Fusion with unsaved changes":** save or close that design, then run again.
- **Cancel:** finished designs are kept, so run again to continue.
- **Check the files:** `python3 scripts/verify-export.py <folder>` (from a download of this repository) opens every archive and STEP file and prints counts only.
- Still stuck? [Open an issue](../../issues/new/choose) with the counts and error messages, with any design names replaced by `X`.

## Unattended export (macOS and Windows)

For large accounts, or to let an agent drive it, the export runs without any dialogs.
You need a download of this repository and Python 3.9 or newer. Sign in to Fusion once, then:

```sh
python3 scripts/autorun.py inventory ~/FusionExport                  # list everything, open nothing
python3 scripts/autorun.py export ~/FusionExport --project PROJECT_ID # try one project first
python3 scripts/autorun.py export ~/FusionExport                      # everything; rerun to resume
python3 scripts/autorun.py status ~/FusionExport                      # counts so far
python3 scripts/autorun.py stop ~/FusionExport                        # pause between designs
```

On Windows use `py scripts\autorun.py ...` and a short folder such as `C:\FusionExport`.

The launcher installs the add-in, hands it the job and starts Fusion if needed.
Fusion does not always start a newly installed add-in by itself. If the launcher says
so, open **Utilities → Add-Ins**, select **FusionBulkExport** and click **Run**, and the
waiting job starts. After that, Fusion stays open and the add-in picks up each new job
at once.

`inventory.json` lists every hub, project and file with its id, so you can choose a
project for a trial run. `--limit N` caps one run. Projects or folders that can't be
read are logged and skipped. `scripts/autorun.py diagnostics <folder>` records which
export functions your Fusion build has, which helps with bug reports. To go back to the
button, quit Fusion and delete `autorun.watch` from the installed add-in folder.

## How resuming works

Running again on the same folder skips a finished design only if its files are still
there with matching checksums. Missing or changed files are made again. A failed design
gets two tries per run. A new version of a design gets its own files next to the old
ones. The manifest is saved after every design, so stopping or a crash loses at most the
design in progress. Run only one export at a time into a folder.

## Checking an export

`python3 scripts/verify-export.py <folder>` checks every exported design:
- the `.f3d` is a readable archive with Fusion's design data;
- the `.step` has a valid start and end;
- the number of solids in the STEP matches the visible solid bodies Fusion reported.

A solid-count difference is printed as a NOTE, not a failure. The count is approximate
for linked components, and Fusion's STEP translator occasionally drops a body it can't
write; the `.f3d` is the complete copy. Use `--strict` to treat differences as failures,
and `--names` to show design names. On Python before 3.14, archive parts compressed with
Zstandard are counted as not CRC-checked.

## Developers

- Tests: `python3 -m unittest discover -s tests` (a simulated Fusion API; no Fusion needed).
- Self-test on a real Mac with a throwaway account: [docs/selftest.md](docs/selftest.md).
- Cards: `python3 docs/cards.py` regenerates the banner and social preview.
- Release zip: `python3 scripts/package.py` writes `dist/FusionBulkExport-<version>.zip`.

MIT licensed; see [LICENSE](LICENSE).

---

Not affiliated with or endorsed by Autodesk. Fusion is a trademark of Autodesk, Inc.
