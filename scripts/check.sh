#!/usr/bin/env bash
set -euo pipefail
mkdir -p out
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
paths = [*Path('FusionBulkExport').rglob('*.py'), *Path('tests').rglob('*.py')]
checksums = {path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
Path('out/source-sha256.json').write_text(json.dumps(checksums, indent=2) + '\n', encoding='utf-8')
PY
python3 -m unittest discover -s tests -v 2>&1 | tee out/tests.txt
python3 -m compileall -q FusionBulkExport tests
