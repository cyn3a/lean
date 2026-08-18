"""Layer 4: free supervised repair labels from Lean/mathlib adaptation diffs."""

__version__ = "0.1.0"

from .diffparse import Hunk, FileDiff, parse_diff        # noqa: F401
from .discover import Window                              # noqa: F401
from .discover import discover as discover_windows        # noqa: F401
from .gitio import Git, clone                             # noqa: F401
from .mine import RepairPair, mine_window                 # noqa: F401
from .mine import mine as mine_windows                    # noqa: F401
from .rules import RepairRule                             # noqa: F401
from .rules import induce as induce_rules                 # noqa: F401
from .rules import synthesize as synthesize_breaks        # noqa: F401
from .taxonomy import RepairLabel, ErrorClass, classify   # noqa: F401
