#!/usr/bin/env python3
"""Copy the public pages from site/ into docs/, which GitHub Pages serves.

GitHub Pages can publish a /docs folder straight from the branch -- no
workflow, no Actions run, one dropdown in the repository settings. That is
the simplest thing that produces a real public URL, so it is what this uses.

site/ stays the source of truth. docs/ is generated, and `--check` fails if
the two have drifted, so an edit to site/ that was never published cannot
sit unnoticed.

desk.html is deliberately not published. It polls a Python process on
localhost for its data, so a public copy would render a permanently
disconnected shell -- a page that cannot work where it is served does not
belong on a public site. data.json is skipped too: app.html already has it
inlined, and shipping it again would double the payload for nothing.

login.html is not published either, and for a sharper reason: a login form
served from a static host has nothing behind it to authenticate against. It
would take a password, POST it into the void, and teach whoever typed it that
this project asks for credentials on a page that cannot check them -- which is
exactly the habit a phishing page relies on. Both pages are served by the desk
process itself (see kbot/webui/server.py) and only ever from there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "site", ROOT / "docs"

#: CNAME is what binds the custom domain to this site, and GitHub Pages reads
PUBLIC = ("index.html", "app.html", "404.html", "robots.txt")

#: Pages that get canonical/social metadata injected, and the path each one
#: is served at.
PAGES = {"index.html": "/", "app.html": "/app.html"}

#: Where the custom domain is declared. Its presence is the ONLY switch: with
#: it, the build emits a CNAME, absolute canonical URLs and a sitemap; without
#: it, none of those appear at all.
#:
#: The order matters and is the whole reason this is a switch rather than a
#: constant. Publishing a CNAME makes Pages 301 the github.io URL to the
#: custom domain -- so if DNS is not yet answering for that domain, adding the
#: CNAME does not "prepare" the site, it takes the working one offline. Set
#: DNS first, confirm it resolves, then create this file.
DOMAIN_FILE = SRC / "CNAME"

#: Shapes that must never reach a public page. A landing page is the easiest
#: place in a project to paste a token by accident.
SECRETS = (
    ("Telegram token", re.compile(rb"[0-9]{6,12}:[A-Za-z0-9_-]{30,}")),
    ("private key", re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY")),
)

META_PLACEHOLDER = "<!--SITE-META-->"


def read_domain() -> str | None:
    if not DOMAIN_FILE.exists():
        return None
    domain = DOMAIN_FILE.read_text().strip()
    return domain or None


def site_meta(domain: str | None, page: str, title: str, description: str) -> str:
    """Canonical and social tags, or nothing at all.

    Nothing is the right answer without a domain: a canonical URL has to be
    absolute to mean anything, and the only absolute URL available would be
    the github.io one -- which is exactly the address the site is meant to
    stop using. Pointing search engines at it would be work to undo later.
    """
    if not domain:
        return ""
    url = f"https://{domain}{page}"
    return "\n".join([
        f'<link rel="canonical" href="{url}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:url" content="{url}">',
        '<meta property="og:site_name" content="DirectionalBot">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{description}">',
        '<meta name="twitter:card" content="summary">',
        f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{description}">',
    ])


TITLES = {
    "index.html": (
        "DirectionalBot",
        "Reads Kalshi's live order book on the 15-minute crypto markets. "
        "No edge is claimed — the numbers are measured, including the "
        "losing ones.",
    ),
    "app.html": (
        "Reversion Desk",
        "Runs the reversion strategy against real recorded Kalshi order "
        "books, scored net of real fees.",
    ),
}


def sitemap(domain: str) -> bytes:
    urls = "\n".join(
        f"  <url>\n    <loc>https://{domain}{path}</loc>\n"
        f"    <changefreq>weekly</changefreq>\n  </url>"
        for path in PAGES.values()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n"
    ).encode()


def build(check: bool = False) -> int:
    OUT.mkdir(exist_ok=True)
    problems: list[str] = []
    domain = read_domain()

    written: dict[str, bytes] = {}

    for name in PUBLIC:
        src = SRC / name
        if not src.exists():
            problems.append(f"missing source: site/{name}")
            continue
        data = src.read_bytes()

        if name in PAGES:
            title, description = TITLES[name]
            meta = site_meta(domain, PAGES[name], title, description)
            text = data.decode()
            if META_PLACEHOLDER not in text:
                problems.append(f"{name} has no {META_PLACEHOLDER} placeholder")
            data = text.replace(META_PLACEHOLDER, meta).encode()

        if name == "robots.txt" and domain:
            data = data.rstrip() + f"\n\nSitemap: https://{domain}/sitemap.xml\n".encode()

        written[name] = data

    # Only with a domain: a sitemap of relative URLs is invalid, and a CNAME
    # without DNS behind it takes the site offline rather than moving it.
    if domain:
        written["sitemap.xml"] = sitemap(domain)
        written["CNAME"] = f"{domain}\n".encode()

    for name, data in written.items():
        for label, pattern in SECRETS:
            if pattern.search(data):
                problems.append(f"{name} contains something shaped like a {label}")

        dest = OUT / name
        if check:
            if not dest.exists() or dest.read_bytes() != data:
                problems.append(f"docs/{name} is stale — run scripts/build-site.py")
        else:
            dest.write_bytes(data)

    # Files that should no longer be published once the domain is switched off.
    for stale in ("CNAME", "sitemap.xml"):
        if stale not in written and (OUT / stale).exists():
            if check:
                problems.append(f"docs/{stale} should not be published without a domain")
            else:
                (OUT / stale).unlink()

    # Stops Pages from hiding files that begin with an underscore.
    nojekyll = OUT / ".nojekyll"
    if check:
        if not nojekyll.exists():
            problems.append("docs/.nojekyll is missing")
    else:
        nojekyll.write_text("")

    if problems:
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    names = ", ".join(written)
    where = f"https://{domain}/" if domain else "the github.io URL"
    print(("checked " if check else "published ") + names)
    print(f"  domain: {domain or 'none — serving from ' + where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(check="--check" in sys.argv))
