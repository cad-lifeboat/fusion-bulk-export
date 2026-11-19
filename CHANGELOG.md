# Changelog

## Unreleased

- Unattended export on macOS and Windows (`scripts/autorun.py`): inventory, per-project trial, resumable full run, status, stop; the add-in keeps watching for new jobs in the same Fusion session.
- AGENTS.md: step-by-step guidance for an AI coding agent running the backup for you.
- Projects or folders that cannot be read are logged and skipped instead of ending the run.
- `scripts/verify-export.py` checks archives and STEP files in an export folder.

## 0.1.0

- First release: export every design in your hubs as .f3z (when it references other designs) or .f3d, plus a STEP of the same version; keeps the project and folder tree; resumable with a manifest.
