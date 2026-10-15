"""Load the plugin package the way Hermes does, for tests run as plain scripts."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "slack_colleague_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
PLUGIN = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = PLUGIN
_SPEC.loader.exec_module(PLUGIN)
from slack_colleague_plugin import slack_tools as TOOLS  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def run_tests(namespace: dict, label: str) -> None:
    tests = [value for name, value in sorted(namespace.items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"{label}=passed tests={len(tests)}")
