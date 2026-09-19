"""Make the repo root importable so tests can reach jev_mcp.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
