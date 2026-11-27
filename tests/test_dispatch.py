"""Runs tools/dispatch/dispatch-tick.sh against a local fake of GitHub's dispatch endpoint."""

import http.server
import json
import os
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools/dispatch/dispatch-tick.sh"


class FakeApi(http.server.BaseHTTPRequestHandler):
    status = 204
    requests: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeApi.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
        self.send_response(FakeApi.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("curl"), "needs curl")
class TestDispatch(unittest.TestCase):
    def setUp(self):
        FakeApi.requests = []
        FakeApi.status = 204
        self.server = http.server.HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_script(self, **env):
        base = {"PATH": os.environ["PATH"], "REPLAY_GITHUB_API": f"http://127.0.0.1:{self.server.server_port}"}
        return subprocess.run(["sh", str(SCRIPT)], env=base | env, capture_output=True, text=True, timeout=60)

    def test_dispatches_a_tick(self):
        p = self.run_script(REPLAY_DISPATCH_TOKEN="secret-token")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(FakeApi.requests), 1)
        r = FakeApi.requests[0]
        self.assertEqual(r["path"], "/repos/snazy/mammoth-replay-controller/actions/workflows/replay.yml/dispatches")
        self.assertEqual(r["auth"], "Bearer secret-token")
        self.assertEqual(r["body"], {"ref": "main", "inputs": {"command": "tick"}})
        self.assertNotIn("secret-token", p.stdout + p.stderr)

    def test_rejected_dispatch_fails(self):
        FakeApi.status = 422
        p = self.run_script(REPLAY_DISPATCH_TOKEN="secret-token")
        self.assertEqual(p.returncode, 1)
        self.assertIn("HTTP 422", p.stderr)
        self.assertNotIn("secret-token", p.stderr)

    def test_missing_token_fails(self):
        p = self.run_script()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("REPLAY_DISPATCH_TOKEN is not set", p.stderr)
        self.assertEqual(FakeApi.requests, [])


if __name__ == "__main__":
    unittest.main()
