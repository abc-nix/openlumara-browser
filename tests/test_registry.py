# Guard for the file split: tools live in mixins but core's loader reads only
# the module class's own __dict__, while webui routes are found through the
# MRO. This test fails if the adoption step stops copying a tool.
#
# Run from anywhere:
#   python3 -m unittest discover -s modules/browser/tests -v

import ast
import builtins
import unittest
from pathlib import Path

from test_module_events import module

MODULE_DIR = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("module.py", "tools_session.py", "tools_page.py", "tools_data.py",
                "events.py", "restart.py", "webui_routes.py")


def _bound_names(tree):
    """Every module-level name the file binds, including imports and args."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names |= set(node.names)
    return names

EXPECTED_TOOLS = {
    "open", "close", "status", "navigate", "snapshot", "click", "type_text",
    "fill_form", "press", "scroll", "back", "screenshot", "evaluate",
    "console", "network_requests", "get_images", "cookies", "list_tabs",
    "new_tab", "switch_tab", "close_tab", "handle_dialog", "cdp",
}
EXPECTED_ROUTES = {"status", "open", "close", "restart", "shot"}


class ToolRegistryTests(unittest.TestCase):
    def test_every_tool_sits_on_the_class_itself(self):
        own = {name for name, member in vars(module.Browser).items()
               if getattr(member, "_is_tool", False)}
        self.assertEqual(own, EXPECTED_TOOLS)

    def test_every_tool_is_guarded(self):
        own = {name for name, member in vars(module.Browser).items()
               if getattr(member, "_is_tool", False)}
        missing = [name for name in own
                   if not getattr(vars(module.Browser)[name], "_is_guarded", False)]
        self.assertEqual(missing, [])

    def test_the_webui_routes_resolve_through_the_mro(self):
        found = set()
        for klass in module.Browser.__mro__:
            for member in vars(klass).values():
                meta = getattr(member, "_ui_route", None)
                if meta:
                    found.add(meta["path"])
        self.assertEqual(found, EXPECTED_ROUTES)


class SourceFilesTests(unittest.TestCase):
    def test_no_source_file_uses_an_unbound_name(self):
        # the split moved helpers between files; a missing import (new_tab's
        # normalize_url) only shows up when that tool is called
        allowed = set(dir(builtins))
        problems = []
        for name in SOURCE_FILES:
            tree = ast.parse((MODULE_DIR / name).read_text(encoding="utf-8"))
            used = {node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
            missing = sorted(used - _bound_names(tree) - allowed)
            if missing:
                problems.append(f"{name}: {', '.join(missing)}")
        self.assertEqual(problems, [], "undefined module-level names")


if __name__ == "__main__":
    unittest.main()
