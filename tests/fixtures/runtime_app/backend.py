"""Local deterministic external interface; SQL counts every mutating request."""

import json
import sqlite3
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

database = Path(".data/projects.sqlite3")
database.parent.mkdir(exist_ok=True)
with sqlite3.connect(database) as db:
    db.execute("CREATE TABLE IF NOT EXISTS requests(kind TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,task TEXT)")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO requests VALUES(?)", (self.path,))
            if self.path == "/create":
                assert not db.execute("SELECT id FROM projects").fetchone(), (
                    "duplicate creation"
                )
                db.execute("INSERT INTO projects VALUES('project-1',NULL)")
            elif self.path == "/submit":
                assert json.loads(Path("capability.json").read_text())["ready"]
                assert not db.execute("SELECT task FROM projects").fetchone()[0], (
                    "duplicate submission"
                )
                db.execute("UPDATE projects SET task='local-provider-1'")
        self.do_GET()

    def do_GET(self):
        with sqlite3.connect(database) as db:
            project = db.execute("SELECT id,task FROM projects").fetchone()
            creates = db.execute(
                "SELECT count(*) FROM requests WHERE kind='/create'"
            ).fetchone()[0]
            submits = db.execute(
                "SELECT count(*) FROM requests WHERE kind='/submit'"
            ).fetchone()[0]
        body = json.dumps(
            {
                "project_id": project[0] if project else None,
                "task_id": project[1] if project else None,
                "creates": creates,
                "submits": submits,
                "ready": json.loads(Path("capability.json").read_text())["ready"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
