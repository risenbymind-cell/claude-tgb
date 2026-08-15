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
    """Present, and non-trivial.

    Byte equality with site/ is deliberately not asserted: the build injects
    canonical and social metadata into the pages and a Sitemap line into
    robots.txt, so the published copy is *derived* from the source rather than
    copied from it. Drift is caught by --check above, which compares against
    a real rebuild instead of against the raw source.
    """
    published = DOCS / name
    assert published.exists(), f"docs/{name} missing"
    assert published.stat().st_size > 0, f"docs/{name} is empty"


def test_the_local_desk_is_never_published():
    """It polls localhost for its data, so a public copy is a permanently
    disconnected shell -- a broken product rather than a page."""
    assert not (DOCS / "desk.html").exists()


# ---------------- the custom domain ----------------
#
# Publishing a CNAME makes Pages 301 the github.io URL to the custom domain.
# So a CNAME added before DNS answers does not prepare the site -- it takes
# the working one offline and replaces it with a redirect into nothing. That
# ordering is the thing these tests exist to protect.


def test_a_cname_is_only_published_alongside_a_domain():
    build = _build_script()
    assert (build.DOMAIN_FILE.exists()) == (DOCS / "CNAME").exists(), (
        "docs/CNAME and site/CNAME must appear and disappear together"
    )


def test_the_root_cname_does_not_come_back():
    """It does nothing at the root -- Pages reads CNAME from the publishing
    source, which is docs/. Having one there invites the belief that the
    domain is configured when it is not."""
    assert not (DOCS.parent / "CNAME").exists()


def test_no_page_advertises_a_domain_that_is_not_configured():
    """A canonical pointing at a host that does not resolve is worse than
    none: it tells search engines the real copy lives somewhere unreachable."""
    build = _build_script()
    if build.read_domain():
        pytest.skip("a domain is configured; covered by the tests below")
    for name in ("index.html", "app.html"):
        html = (DOCS / name).read_text()
        assert "canonical" not in html, name
        assert "og:url" not in html, name
    assert not (DOCS / "sitemap.xml").exists()
    assert "Sitemap:" not in (DOCS / "robots.txt").read_text()


def test_the_placeholder_is_always_substituted():
    """A literal <!--SITE-META--> reaching a visitor means the build did not
    run, which is silent in a way a broken tag is not."""
    for name in ("index.html", "app.html"):
        assert "<!--SITE-META-->" not in (DOCS / name).read_text(), name


def test_configuring_a_domain_wires_everything_to_it(tmp_path, monkeypatch):
    """The switch has to light up every piece at once -- a CNAME without
    canonicals, or canonicals without a sitemap, is a half-migrated site."""
    import xml.etree.ElementTree as ET

    build = _build_script()
    monkeypatch.setattr(build, "OUT", tmp_path)
    monkeypatch.setattr(build, "DOMAIN_FILE", tmp_path / "CNAME")
    (tmp_path / "CNAME").write_text("example.test\n")

    assert build.build() == 0

    assert (tmp_path / "CNAME").read_text().strip() == "example.test"
    for name, path in (("index.html", "/"), ("app.html", "/app.html")):
        html = (tmp_path / name).read_text()
        assert f'rel="canonical" href="https://example.test{path}"' in html, name
    assert "Sitemap: https://example.test/sitemap.xml" in (
        tmp_path / "robots.txt"
    ).read_text()

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [
        e.text
        for e in ET.parse(tmp_path / "sitemap.xml").getroot().findall(".//s:loc", ns)
    ]
    assert locs, "sitemap parsed but found no URLs -- check the namespace"
    for loc in locs:
        page = loc.rsplit("/", 1)[1] or "index.html"
        assert (tmp_path / page).exists(), f"{loc} would be a 404"


def test_removing_the_domain_takes_the_cname_with_it(tmp_path, monkeypatch):
    """Otherwise a stale CNAME keeps redirecting the site to a domain nobody
    is serving any more."""
    build = _build_script()
    monkeypatch.setattr(build, "OUT", tmp_path)
    monkeypatch.setattr(build, "DOMAIN_FILE", tmp_path / "CNAME")

    (tmp_path / "CNAME").write_text("example.test\n")
    build.build()
    assert (tmp_path / "CNAME").exists()

    (tmp_path / "CNAME").unlink()
    build.build()
    assert not (tmp_path / "CNAME").exists()
    assert not (tmp_path / "sitemap.xml").exists()


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
