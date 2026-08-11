import sys
from pathlib import Path

# Allow `pytest` from the repo root without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
