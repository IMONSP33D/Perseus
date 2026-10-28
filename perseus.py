#!/usr/bin/env python3
"""Start Perseus: `python perseus.py` after `bash preflight.sh`.

Runs under the project's .venv whichever python you call it with.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"

if __name__ == "__main__":
    if VENV_PY.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(ROOT))
    from perseus.launcher import main
    main()
