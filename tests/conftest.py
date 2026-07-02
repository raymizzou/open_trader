from __future__ import annotations

import sys
from pathlib import Path


PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
project_src = str(PROJECT_SRC)
if sys.path[0] != project_src:
    if project_src in sys.path:
        sys.path.remove(project_src)
    sys.path.insert(0, project_src)
