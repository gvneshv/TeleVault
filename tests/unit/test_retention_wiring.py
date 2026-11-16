"""
main.py really starts the retention job and stops it on shutdown.

main() needs a live Telegram client and a database, so it is not run here; instead the source is read as a syntax tree.
This is a deliberately modest check - it proves the job is wired in (a forgotten line would silently mean "automatic cleanup never runs"),
not that the job works (tests/integration/test_retention_db.py does that).
"""

import ast
import inspect

import main


def _calls(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_the_archiver_starts_the_retention_loop_as_a_task():
    tree = ast.parse(inspect.getsource(main))
    started = [
        c for c in _calls(tree)
        if _dotted(c.func) == "asyncio.create_task" and c.args and isinstance(c.args[0], ast.Call) and _dotted(c.args[0].func) == "retention.retention_loop"
    ]
    assert len(started) == 1


def test_the_retention_task_is_cancelled_on_shutdown():
    tree = ast.parse(inspect.getsource(main))
    assert any(_dotted(c.func) == "retention_task.cancel" for c in _calls(tree))
