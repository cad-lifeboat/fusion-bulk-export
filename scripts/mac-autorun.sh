#!/usr/bin/env bash
# Unattended export on macOS. Run from a logged-in desktop session with Fusion
# signed in and closed. Usage:
#   mac-autorun.sh OUTPUT_FOLDER [inventory|export] [PROJECT_ID ...]
# Environment: AUTORUN_LIMIT (max designs), AUTORUN_TIMEOUT_SECONDS (default 86400),
# FUSION_APP (app name or .app path, default "Autodesk Fusion").
set -euo pipefail
if [[ $(uname -s) != Darwin ]]; then
  echo 'This launcher requires macOS.' >&2
  exit 2
fi
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 OUTPUT_FOLDER [inventory|export] [PROJECT_ID ...]" >&2
  exit 2
fi
for process in 'Autodesk Fusion' 'Autodesk Fusion 360' Fusion360; do
  if pgrep -x "$process" >/dev/null; then
    echo 'Quit Fusion before running this script so startup add-ins run.' >&2
    exit 2
  fi
done
repo=$(cd "$(dirname "$0")/.." && pwd)
output=$1
mode=${2:-export}
shift $(( $# >= 2 ? 2 : 1 ))
installed="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionBulkExport"
python3 - "$repo" "$output" "$mode" "$installed" "${AUTORUN_LIMIT:-}" "$@" <<'PY'
import json
from pathlib import Path
import shutil
import sys
repo, output, mode, installed, limit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5]
projects = sys.argv[6:]
output = output.expanduser().resolve()
if len(str(output).encode('utf-16-le')) // 2 > 100:
    raise SystemExit('Use an output path of at most 100 characters.')
if mode not in ('export', 'inventory'):
    raise SystemExit('Mode must be export or inventory.')
output.mkdir(parents=True, exist_ok=True)
for stale in ('autorun-result.json', 'STOP'):
    (output / stale).unlink(missing_ok=True)
shutil.copytree(repo / 'FusionBulkExport', installed, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', 'selftest.json*', 'autorun.json*', '*-output'))
config = dict(output=str(output), mode=mode)
if projects:
    config['projects'] = projects
if limit:
    config['limit'] = int(limit)
(installed / 'autorun.json').write_text(json.dumps(config, indent=2) + '\n')
PY
set_startup() {
  python3 - "$installed/FusionBulkExport.manifest" "$1" <<'PY'
import json
from pathlib import Path
import sys
manifest = Path(sys.argv[1])
settings = json.loads(manifest.read_text())
settings['runOnStartup'] = sys.argv[2] == 'on'
manifest.write_text(json.dumps(settings, indent=2) + '\n')
PY
}
set_startup on
trap 'set_startup off' EXIT
open -a "${FUSION_APP:-Autodesk Fusion}"
python3 - "$output" "${AUTORUN_TIMEOUT_SECONDS:-86400}" <<'PY'
import json
from pathlib import Path
import sys
import time
output, timeout = Path(sys.argv[1]).expanduser().resolve(), float(sys.argv[2])
result = output / 'autorun-result.json'
print('Waiting for ' + str(result) + ' (progress: autorun.log; create STOP there to pause)', flush=True)
deadline = time.monotonic() + timeout
while not result.is_file():
    if time.monotonic() >= deadline:
        raise SystemExit('Timed out. Inspect ' + str(output / 'autorun.log'))
    time.sleep(5)
report = json.loads(result.read_text())
print('Mode:', report['mode'], 'Fusion:', report['fusion_version'])
print('Counts:', json.dumps(report.get('counts', {})))
for key in ('found', 'cancelled', 'discovery_errors', 'bytes'):
    if key in report:
        print(key + ':', report[key])
print('Errors:', len(report['errors']))
# Per-design failures are in the manifest; only a run-level error is a failure here.
sys.exit(1 if any(e['stage'] == 'autorun' for e in report['errors']) else 0)
PY
