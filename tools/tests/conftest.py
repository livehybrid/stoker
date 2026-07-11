import sys
from pathlib import Path

# Make tools/ importable so tests can import hec_sink directly.
TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
