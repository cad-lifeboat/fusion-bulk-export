#!/usr/bin/env bash
# Run from a logged-in macOS desktop session with Fusion closed and signed in.
set -euo pipefail
if [[ $(uname -s) != Darwin ]]; then
  echo 'This launcher requires macOS.' >&2
  exit 2
fi
if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: $0 SEED_FOLDER [OUTPUT_FOLDER [PROJECT]]" >&2
  exit 2
fi
command -v python3 >/dev/null
for process in 'Autodesk Fusion' 'Autodesk Fusion 360' Fusion360; do
  if pgrep -x "$process" >/dev/null; then
    echo 'Quit Fusion before running this script so startup add-ins run.' >&2
    exit 2
  fi
done
repo=$(cd "$(dirname "$0")/.." && pwd)
seed=$1
output=${2:-$(mktemp -d /tmp/fusion-selftest.XXXXXX)}
project=${3:-bulk-export-selftest}
installed="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionBulkExport"
python3 - "$repo" "$seed" "$output" "$project" "$installed" <<'PY'
import json
from pathlib import Path
import shutil
import sys
repo, seed, output, installed = (Path(sys.argv[i]) for i in (1, 2, 3, 5))
project = sys.argv[4]
seed, output = seed.expanduser().resolve(), output.expanduser().resolve()
if not seed.is_dir() or not any(p.is_file() and p.suffix.lower() in {'.f3d', '.f3z'} for p in seed.rglob('*')):
    raise SystemExit('Seed folder must contain .f3d or .f3z files.')
if len(str(output).encode('utf-16-le')) // 2 > 100:
    raise SystemExit('Use an output path of at most 100 characters.')
output.mkdir(parents=True, exist_ok=True)
if any(output.iterdir()):
    raise SystemExit('Use a new, empty output folder for fresh test evidence.')
shutil.copytree(repo / 'FusionBulkExport', installed, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', 'selftest.json', 'selftest-output'))
(installed / 'selftest.json').write_text(json.dumps(dict(
    output=str(output), seed_folder=str(seed), project=str(project)), indent=2) + '\n')
PY
# Load at startup for this test only; the shipped manifest keeps runOnStartup off.
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
# LaunchServices resolves the default registered app; FUSION_APP may be a .app path.
open -a "${FUSION_APP:-Autodesk Fusion}"
python3 - "$output" "${SELFTEST_TIMEOUT_SECONDS:-3600}" <<'PY'
import json
from pathlib import Path
import sys
import time
output = Path(sys.argv[1]).expanduser().resolve()
timeout = float(sys.argv[2])
if timeout <= 0:
    raise SystemExit('SELFTEST_TIMEOUT_SECONDS must be positive.')
result = output / 'selftest-result.json'
print('Waiting for ' + str(result), flush=True)
deadline = time.monotonic() + timeout
while not result.is_file():
    if time.monotonic() >= deadline:
        raise SystemExit('Timed out. Inspect ' + str(output / 'selftest.log') +
                         ' (or the installed add-in/selftest-output for configuration errors).')
    time.sleep(2)
report = json.loads(result.read_text())
print('PASS' if report['passed'] else 'FAIL')
print('Scope:', report.get('scope'))
print('Fusion:', report['fusion_version'])
print('Counts:', report.get('counts', {}))
for design in report['designs']:
    print(design['name'], design['status'], 'archive correct:', design['archive_correct'])
for error in report['errors']:
    print(error['stage'] + ': ' + error['error'])
print('Evidence:', output)
sys.exit(0 if report['passed'] else 1)
PY
