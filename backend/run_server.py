from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_LIBS = ROOT_DIR / ".pythonlibs"

sys.path.insert(0, str(ROOT_DIR))

if LOCAL_LIBS.exists():
    sys.path.insert(0, str(LOCAL_LIBS))

import uvicorn


if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=4000)
