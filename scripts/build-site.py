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
#: it from the *publishing source* -- which here is docs/, not the repository
#: root. A CNAME at the root is silently ignored, which is a bad failure: the
#: file exists, the settings look right, and the domain simply does not work.
#: Publishing it from site/ like any other page means a rebuild cannot drop it
#: either -- and a rebuild that dropped it would take the live domain down.
PUBLIC = ("index.html", "app.html", "404.html", "robots.txt", "sitemap.xml", "CNAME")

#: Shapes that must never reach a public page. A landing page is the easiest
#: place in a project to paste a token by accident.
SECRETS = (
    ("Telegram token", re.compile(rb"[0-9]{6,12}:[A-Za-z0-9_-]{30,}")),
    ("private key", re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY")),
)


def build(check: bool = False) -> int:
    OUT.mkdir(exist_ok=True)
    problems: list[str] = []

    for name in PUBLIC:
        src = SRC / name
        if not src.exists():
            problems.append(f"missing source: site/{name}")
            continue
        data = src.read_bytes()

        for label, pattern in SECRETS:
            if pattern.search(data):
                problems.append(f"{name} contains something shaped like a {label}")

        dest = OUT / name
        if check:
            if not dest.exists() or dest.read_bytes() != data:
                problems.append(f"docs/{name} is stale — run scripts/build-site.py")
        else:
            dest.write_bytes(data)

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

    print(("checked " if check else "published ") + ", ".join(PUBLIC))
    return 0


if __name__ == "__main__":
    raise SystemExit(build(check="--check" in sys.argv))
