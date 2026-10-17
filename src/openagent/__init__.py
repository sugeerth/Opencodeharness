"""Open Agent: a verify-and-repair harness that makes OpenCode finish the job."""
__version__ = "0.3.0"

from .agent import PRESETS, Strategy, solve  # noqa: E402,F401
from .opencode import OpenCode  # noqa: E402,F401
