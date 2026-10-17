"""
The dev docker-compose file and its init scripts must agree with the app's default configuration.

Regression: the compose file used to create a database named `televault` that nothing used, next to the real `televault_control`.
"""

import re
from pathlib import Path

from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
DEFAULT_CONTROL_DB = make_url(re.search(r'"(postgresql\+psycopg://[^"]+)"', (REPO / "config.py").read_text(encoding="utf-8")).group(1)).database


def _compose_value(key: str) -> str:
    # Plain-text scan (no YAML dependency): the first `KEY: value` line, minus any trailing comment.
    match = re.search(rf"^\s*{key}:\s*([^\s#]+)", COMPOSE, re.MULTILINE)
    assert match, f"{key} not found in docker-compose.yml"
    return match.group(1)


def test_the_only_database_the_container_creates_is_the_control_database():
    assert _compose_value("POSTGRES_DB") == DEFAULT_CONTROL_DB == "televault_control"


def test_the_healthcheck_probes_that_same_database():
    assert f"-d {DEFAULT_CONTROL_DB}" in COMPOSE


def test_the_user_matches_the_default_connection_url():
    assert _compose_value("POSTGRES_USER") == make_url(re.search(r'"(postgresql\+psycopg://[^"]+)"', (REPO / "config.py").read_text(encoding="utf-8")).group(1)).username


def test_no_init_script_creates_another_database():
    for script in (REPO / "docker" / "init").glob("*.sql"):
        assert not re.search(r"^\s*CREATE\s+DATABASE\b", script.read_text(encoding="utf-8"), re.IGNORECASE | re.MULTILINE), script.name
