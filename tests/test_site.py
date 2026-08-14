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


def _build_script():
    """Load scripts/build-site.py, whose hyphen makes it unimportable."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "build-site.py"
    spec = importlib.util.spec_from_file_location("build_site", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Taken from the build script rather than restated, so a file added to the
#: published set cannot be missed by these tests.
PUBLIC = _build_script().PUBLIC


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


DOMAIN = "konneh.bot"


def test_the_custom_domain_is_bound_where_pages_reads_it():
    """GitHub Pages reads CNAME from the *publishing source*, which here is
    docs/ -- a CNAME at the repository root is silently ignored. That is a bad
    failure mode: the file exists, the settings look right, and the domain
    just does not work."""
    assert (DOCS / "CNAME").exists(), "docs/CNAME is what binds the domain"
    assert (DOCS / "CNAME").read_text().strip() == DOMAIN


def test_a_rebuild_cannot_drop_the_domain():
    """CNAME is published like any other page rather than written by hand, so
    regenerating docs/ cannot take the live domain down."""
    assert "CNAME" in PUBLIC
    assert (SITE / "CNAME").exists(), "site/ is the source of truth"


def test_the_root_cname_does_not_come_back():
    """It does nothing at the root, and having one there invites the belief
    that the domain is configured when it is not."""
    assert not (DOCS.parent / "CNAME").exists()


def test_the_canonical_urls_point_at_the_real_domain():
    """Relative canonicals would resolve to whichever host served the page --
    including the github.io one, which then competes with the domain."""
    for name, path in (("index.html", "/"), ("app.html", "/app.html")):
        html = (DOCS / name).read_text()
        assert f'rel="canonical" href="https://{DOMAIN}{path}"' in html, name


def test_the_sitemap_lists_only_pages_that_exist():
    import xml.etree.ElementTree as ET

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(DOCS / "sitemap.xml")
    locs = [e.text for e in tree.getroot().findall(".//s:loc", ns)]
    assert locs, "sitemap parsed but found no URLs -- check the namespace"
    for loc in locs:
        assert loc.startswith(f"https://{DOMAIN}/")
        page = loc.rsplit("/", 1)[1] or "index.html"
        assert (DOCS / page).exists(), f"{loc} is a 404"


def test_robots_points_at_the_sitemap():
    assert f"Sitemap: https://{DOMAIN}/sitemap.xml" in (DOCS / "robots.txt").read_text()


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
