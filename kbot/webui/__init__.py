"""Local web desk: a browser UI served by this process.

Kalshi answers 403 to any request carrying a browser `Origin` header, so a page
cannot call it directly. The browser talks to localhost; this process talks to
Kalshi.
"""

from .desk import Desk
from .server import DeskServer

__all__ = ["Desk", "DeskServer"]
