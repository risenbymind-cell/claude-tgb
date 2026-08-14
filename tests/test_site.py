"""The published site.

site/ is the source; docs/ is what GitHub Pages serves. The risk in that
arrangement is drift -- an edit to site/ that never reaches docs/ looks fine
in the repository and is invisible to every visitor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path("docs")
SITE = Path("site")
PUBLIC = ("index.html", "app.html", "404.html", "robots.txt")


def test_the_published_copy_matches_the_source():
    """docs/ is generated from site/. An edit to site/ that never reached
    docs/ looks fine in the repository and is invisible to every visitor,
    so the check runs the real script rather than reimplementing it."""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "scripts/build-site.py", "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", PUBLIC)
def test_every_public_page_is_published(name):
    assert (DOCS / name).exists(), f"docs/{name} missing"
    assert (DOCS / name).read_bytes() == (SITE / name).read_bytes()


def test_the_local_desk_is_never_published():
    """It polls localhost for its data, so a public copy is a permanently
    disconnected shell -- a broken product rather than a page."""
    assert not (DOCS / "desk.html").exists()


def test_the_login_page_is_never_published():
    """A login form on a static host has nothing to authenticate against. It
    would take a password, POST it into the void, and teach whoever typed it
    that this project asks for credentials on a page that cannot check them --
    the exact habit a phishing page relies on."""
    assert not (DOCS / "login.html").exists()


def test_no_secret_shaped_string_is_published():
    """A landing page is the easiest place in a project to paste a token."""
    for name in PUBLIC:
        data = (DOCS / name).read_bytes()
        assert not re.search(rb"[0-9]{6,12}:[A-Za-z0-9_-]{30,}", data), name
        assert b"BEGIN RSA PRIVATE KEY" not in data, name


def test_nojekyll_is_present():
    """Without it Pages hides anything beginning with an underscore."""
    assert (DOCS / ".nojekyll").exists()


def test_the_pages_link_to_each_other():
    index = (DOCS / "index.html").read_text()
    assert 'href="/app.html"' in index, "the simulator must not be an orphan"
    assert 'href="/"' in (DOCS / "app.html").read_text()
