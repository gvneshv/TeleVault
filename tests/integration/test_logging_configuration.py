"""
Logging must actually reach the console in the API server, and must survive an in-process archive provisioning.

Regression: the API process never called setup_logging(),
so uvicorn (which configures only its own loggers) left the root logger at WARNING and every INFO line from api.*, db.* and control_db.* disappeared.
Tests that use caplog do NOT catch this - caplog sets its own level - so these tests look at the real root logger instead.
"""

import logging

import pytest

import api.server as server


@pytest.fixture
def pristine_root_logger():
    """
    A root logger in the state a freshly started `uvicorn` process has: no handlers, level WARNING. Restores everything afterwards.

    pytest re-attaches its own capture handlers to the root logger at the start of each test phase,
    and basicConfig() does nothing when handlers exist - so the handlers are cleared HERE only to restore them later;
    each test calls `reset()` itself, right before it configures logging.
    """
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)

    def reset():
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        return root

    yield reset
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def test_the_api_lifespan_configures_logging(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "setup_logging", lambda **kwargs: calls.append(kwargs))
    # `client` already ran the lifespan once before the patch; run it again with the patch in place.
    from fastapi.testclient import TestClient

    with TestClient(server.app):
        pass
    assert calls == [{"log_level": server.settings.log_level, "log_file": server.settings.log_file}]


def test_setup_logging_makes_info_from_our_modules_visible(pristine_root_logger, capsys):
    from utils.logging_setup import setup_logging

    pristine_root_logger()
    setup_logging(log_level="INFO")
    logging.getLogger("api.routes.telegram").info("visible-info-line")
    assert "visible-info-line" in capsys.readouterr().out


def test_without_setup_logging_info_is_dropped(pristine_root_logger, capsys):
    """Documents the original failure mode, so the test above is known to be meaningful."""
    pristine_root_logger()
    logging.getLogger("api.routes.telegram").info("dropped-info-line")
    out = capsys.readouterr()
    assert "dropped-info-line" not in out.out + out.err


def test_provisioning_leaves_the_servers_logging_configuration_alone(server_ready, pristine_root_logger):
    """alembic.ini's [logger_root] (WARNING + its own handler) must not replace the running server's configuration."""
    from db.provisioning import provision_archive_database
    from utils.logging_setup import setup_logging

    root = pristine_root_logger()
    setup_logging(log_level="INFO")
    handlers_before = root.handlers[:]
    provision_archive_database(900_003)
    assert root.level == logging.INFO
    assert root.handlers == handlers_before