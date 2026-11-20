# Contributing

Run the tests before sending a change: `python3 -m unittest discover -s tests` (Python 3.9 or newer;
a simulated Fusion API, no Fusion needed) and `node tests/tree_logic_test.js`. CI runs them on macOS,
Windows and Linux. Build the release zip with `python3 scripts/package.py`, install a checkout into
Fusion with `python3 scripts/install.py`.

**Privacy:** never put real hub, project, design or user names in issues, pull requests, tests,
screenshots or commit messages. Use made-up names, and replace your user name in paths with `X`. CI's
privacy scan runs locally too: `python3 scripts/privacy_scan.py`.
