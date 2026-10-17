"""
The static-file mount must give unknown /api/ paths a JSON 404 on every operating system.

Regression: Starlette hands StaticFiles.get_response() a path built with os.path.normpath(), so on Windows "/api/nope" arrives as "api\\nope".
The guard compared against "api/" only, so on Windows every typo'd API path got the HTML 404 page instead.
These tests call the method with both spellings, so the Windows behaviour is covered even when the suite runs on Linux (where CI runs).
"""

import asyncio

import pytest
from starlette.exceptions import HTTPException

from api.server import _WEB_DIR, _WebStaticFiles

SCOPE = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "root_path": ""}


def _get(path: str):
    mount = _WebStaticFiles(directory=_WEB_DIR, html=True)
    return asyncio.run(mount.get_response(path, SCOPE))


@pytest.mark.parametrize("path", ["api", "api/nope", "api/a/b", "api\\nope", "api\\a\\b", "api\\a/b"])
def test_unknown_api_paths_raise_the_plain_404_with_either_separator(path):
    with pytest.raises(HTTPException) as exc:
        _get(path)
    assert exc.value.status_code == 404


def test_other_unknown_paths_still_get_the_html_404_page():
    response = _get("definitely-not-a-page")
    assert response.status_code == 404 and "text/html" in response.headers["content-type"]


def test_a_path_that_merely_starts_with_api_is_not_an_api_path():
    # "apidocs.html" is not under /api/, so it must fall through to the normal static handling (404 page here, since no such file exists).
    response = _get("apidocs.html")
    assert response.status_code == 404 and "text/html" in response.headers["content-type"]
