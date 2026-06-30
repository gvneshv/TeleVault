"""
Migration runner.
Each numbered module in this package contains a single `run(conn)` function that applies one idempotent schema change.

Migrations are applied in filename order on every startup, after the base DDL in schema.py.
Because each migration checks for its own preconditions before altering anything, re-running is safe.

Adding a new migration:
    1. Create `NNN_short_description.py` in this directory.
    2. Implement `run(conn: sqlite3.Connection) -> None`.
    3. That's it — the runner picks it up automatically.
"""

import importlib
import logging
import pkgutil
import sqlite3

logger = logging.getLogger(__name__)


def run_all(conn: sqlite3.Connection) -> None:
    """
    Discover and run all migration modules in this package, in order.

    Modules are sorted by filename so NNN_ prefixes guarantee ordering.
    Each module's `run()` is responsible for its own idempotency check.
    """
    package_path = __path__  # type: ignore[name-defined]
    package_name = __name__

    modules = sorted(
        info.name
        for info in pkgutil.iter_modules(package_path)
        if not info.name.startswith("_")
    )

    for module_name in modules:
        full_name = f"{package_name}.{module_name}"
        module = importlib.import_module(full_name)
        if hasattr(module, "run"):
            logger.debug("Running migration: %s", module_name)
            module.run(conn)
        else:
            logger.warning(
                "Migration module %s has no run() function — skipped.", module_name
            )