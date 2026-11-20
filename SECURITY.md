# Security

## Reporting a problem

If you find a security problem, please report it privately: on GitHub open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue for it. Describe
what you found and how to reproduce it, without your own project or design names. You should get a
reply within a week.

## What the add-in does

- **Reads only.** It lists your hubs, projects and folders, opens each design and saves a copy
  (`.f3d` archive, `.step` and thumbnail) to a folder on your computer that you choose. Other
  files are downloaded as they are, and drawings (only when you tick that option) are opened and
  saved as PDF. It never uploads,
  saves, renames, moves or deletes anything in your Fusion account. A test checks that the release
  zip contains no call that could.
- **No network calls of its own.** All access to your account goes through Fusion's own API in the
  Fusion session you are signed in to. The add-in does not contact any other server.
- **No telemetry.** Nothing is collected or sent anywhere. The manifest, the report and logs
  stay in your output folder, `errors.log` in the add-in folder, and the dialog's remembered
  settings (last output folder and choices) in `FusionBulkExport/settings.json` in your user's
  application-data folder.
- **Keeping the computer awake.** During an export it runs macOS's `caffeinate` (or, on Windows,
  asks the system not to sleep) and stops that when the export ends.
- **Installers.** `Install (macOS).command` and `Install (Windows).bat` only copy the
  `FusionBulkExport` folder into Fusion's add-ins folder for your user. They need no administrator
  rights and change nothing else. Both are short plain-text scripts: read them before running.

The files it writes (`manifest.json`, `index.html`, `index.csv`, `inventory.json`, logs) contain
your project and design names.
Treat them as private.

`errors.log` in the add-in folder, and the error text the dialog offers to copy, are the
exception: they hold the error's type, its message with paths and quoted text left out, and the
lines of the add-in's own code it passed through, but no paths and no hub, project or design
names. They are meant to be pasted into an issue; still read them once before you post.
While an export runs, `export.lock` in the output folder holds the process id, the computer's
name and the start time, so that a second export cannot use the same folder.
