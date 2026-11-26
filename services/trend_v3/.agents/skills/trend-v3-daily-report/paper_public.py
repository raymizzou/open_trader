"""Load the canonical allowlist from this exact immutable skill release."""

import importlib.util
from pathlib import Path

# Do not resolve through sys.path: an installed package or daily-workspace module
# must not replace the renderer belonging to the selected standalone skill.
_path = Path(__file__).resolve().parents[3] / "src/trend_v3/paper_public.py"
_spec = importlib.util.spec_from_file_location("_pinned_paper_public", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
PAPER_PLACEHOLDER = _module.PAPER_PLACEHOLDER
render_simulation = _module.render_simulation
