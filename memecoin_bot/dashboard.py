"""Read-only local dashboard server for the paper bot.

    python dashboard.py            # http://127.0.0.1:8000
    python dashboard.py --port 9000 --state state.json --journal journal.csv

Serves site/ plus /state.json and /journal.csv. Binds to localhost only.
"""
import argparse
import functools
import http.server
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    state: Path
    journal: Path

    def do_GET(self):
        files = {"/state.json": (self.state, "application/json"),
                 "/journal.csv": (self.journal, "text/csv")}
        if self.path.split("?")[0] in files:
            path, ctype = files[self.path.split("?")[0]]
            body = path.read_bytes() if path.exists() else (b"{}" if ctype.endswith("json") else b"")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--journal", default="journal.csv")
    a = ap.parse_args()
    Handler.state, Handler.journal = Path(a.state), Path(a.journal)
    handler = functools.partial(Handler, directory=str(HERE / "site"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print(f"Dashboard on http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
