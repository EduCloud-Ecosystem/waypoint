"""Explicit trusted checkout bootstrap for one isolated, offline test selection."""
from pathlib import Path
import sys
import unittest

ROOT = Path("/Users/gregchism/dev/.educloud-worktrees/waypoint-streaming-summary/research-workspace")
if not sys.flags.isolated or not sys.dont_write_bytecode:
    raise SystemExit("isolated/no-bytecode parent required")
sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_streaming*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
