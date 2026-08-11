"""Lekkie testy kontraktu API klienta, bez uruchamiania serwera i Supabase."""

import ast
from pathlib import Path


def _app_tree():
    return ast.parse(Path("app.py").read_text(encoding="utf-8"))


def test_client_order_endpoint_exists():
    paths = set()
    for node in ast.walk(_app_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and decorator.args:
                arg = decorator.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    paths.add(arg.value)
    assert "/api/client/orders" in paths


def test_netlify_adapter_is_kept_outside_tests():
    adapter = Path("netlify/functions/flask_api.py")
    assert adapter.exists()
    assert "def handler(event, context)" in adapter.read_text(encoding="utf-8")
