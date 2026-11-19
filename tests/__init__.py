"""Make the installed add-in directory importable for synthetic tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FusionBulkExport'))
