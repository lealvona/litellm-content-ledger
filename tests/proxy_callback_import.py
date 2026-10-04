"""Import the proxy callback the way LiteLLM does: as a standalone file, not through the package."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "src" / "content_ledger" / "proxy_callback.py"
_spec = importlib.util.spec_from_file_location("content_ledger_standalone", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

globals().update({k: getattr(_module, k) for k in dir(_module) if not k.startswith("__")})
