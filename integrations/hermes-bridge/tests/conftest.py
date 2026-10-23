"""pytest wiring for `python3 -I -m pytest`: isolated mode adds neither cwd nor PYTHONPATH.

The tests import the bridge services as top-level modules (`import bridge`), as the
original unittest invocation did with PYTHONPATH=.:tests. Suites that need the real
Hermes host (`hermes_cli`, `hermes_state`) are skipped, visibly, when it is absent.
"""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'tests'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

collect_ignore = []
if importlib.util.find_spec('hermes_cli') is None:
    # Real-host integration suites; they need the staged Hermes host checkout on sys.path.
    collect_ignore += ['test_plugin_enabled.py', 'test_broker_host.py']
