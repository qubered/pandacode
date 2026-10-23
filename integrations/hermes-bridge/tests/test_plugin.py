"""Plugin boundary tests. Context double represents the documented host facade only."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_pandacode_bridge', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])


class Context:
    profile_name = 'default'

    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.cleanups = []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.cleanups.append(callback)

    def inject_message(self, *args, **kwargs):
        raise AssertionError('unsafe host injection must never be attempted')

    def call_mcp(self, *args, **kwargs):
        raise AssertionError('disabled plugin must not call MCP')


class PluginTests(unittest.TestCase):
    def test_register_is_inert_and_launch_is_explicitly_blocked(self):
        self.assertTrue((ROOT / '__init__.py').exists(), 'plugin entrypoint is not implemented')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        ctx = Context()
        module.register(ctx)
        self.assertEqual(set(ctx.tools), {'pandacode_bridge_launch'})
        self.assertEqual(set(ctx.hooks), {'pre_llm_call'})
        ctx.hooks['pre_llm_call'](session_id='irrelevant')
        output = json.loads(ctx.tools['pandacode_bridge_launch']['handler']({}))
        self.assertEqual(output['error'], 'capture_only_not_enabled')
        for callback in reversed(ctx.cleanups):
            callback()
