# Changelog

## Unreleased

- Unattended export on macOS (`scripts/mac-autorun.sh`): inventory, per-project trial, resumable full run, STOP file to pause.
- Projects or folders that cannot be read are logged and skipped instead of ending the run.
- `scripts/verify-export.py` checks archives and STEP files in an export folder.

## 0.1.0

- First release: export every design in your hubs as .f3z (when it references other designs) or .f3d, plus a STEP of the same version; keeps the project and folder tree; resumable with a manifest.
