"""Test isolation for the whole suite.

The registry keeps its runtime files (source_registry.json and its history)
in SIGNALYTH_RUNTIME_CONFIG_DIR, which defaults to the repository's own
config/ folder. Every full test run therefore rewrote
config/source_registry_history.json in the working tree — noise that dirtied
git after every pytest invocation. Point the runtime config at a throwaway
directory BEFORE app.config/app.registry are imported (their paths are
resolved at import time), so tests exercise the same code against their own
copy and the repository files stay untouched.
"""
from __future__ import annotations

import os
import tempfile

_RUNTIME_DIR = tempfile.mkdtemp(prefix="signalyth-test-config-")
os.environ.setdefault("SIGNALYTH_RUNTIME_CONFIG_DIR", _RUNTIME_DIR)
