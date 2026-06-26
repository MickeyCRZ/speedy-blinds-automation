"""                                                      
conftest.py — pytest configuration for speedy_blinds tests
===========================================================
Adds the parent directory (speedy_blinds/) to sys.path so all modules
(config, erp, parser, sheets) are importable without installation.
Also mocks heavy third-party SDKs at import time so parser.py, sheets.py
and erp.py can be imported without credentials or installed packages.
"""

from __future__ import annotations  # must be first

import sys
import types as _types
import unittest.mock as _mock
from pathlib import Path
from unittest.mock import MagicMock

# Make speedy_blinds/ importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Stub out third-party packages unavailable in the bare test environment
# ---------------------------------------------------------------------------

# dotenv — config.py does `from dotenv import load_dotenv`
_dotenv_mod = _types.ModuleType("dotenv")
_dotenv_mod.load_dotenv = lambda *a, **kw: None
sys.modules["dotenv"] = _dotenv_mod

# requests — erp.py does `import requests`
sys.modules.setdefault("requests", _mock.MagicMock())

# requests.exceptions — used in erp.py network error handling
_req_mod = sys.modules["requests"]
import types
_exc_mod = types.ModuleType("requests.exceptions")
class _ConnectionError(OSError): pass
class _RequestException(OSError): pass
_exc_mod.ConnectionError = _ConnectionError
_exc_mod.RequestException = _RequestException
sys.modules["requests.exceptions"] = _exc_mod
_req_mod.exceptions = _exc_mod
_req_mod.RequestException = _RequestException

# tabulate — used in main.py (not tested but imported)
sys.modules.setdefault("tabulate", _mock.MagicMock())

# Google SDKs
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.service_account", MagicMock())
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

# Provide a real HttpError stub so sheets tests can raise and catch it
_errors_mod = _types.ModuleType("googleapiclient.errors")

class _HttpError(Exception):
    def __init__(self, resp=None, content=b"", uri=None):
        self.resp = resp or {}
        self.content = content
        super().__init__(str(content))

_errors_mod.HttpError = _HttpError
sys.modules["googleapiclient.errors"] = _errors_mod


