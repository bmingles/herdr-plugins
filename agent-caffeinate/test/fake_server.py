"""A fake Herdr server: enough of the socket protocol to drive the daemon offline.

Fidelity that matters, both measured against a real 0.8.2 server:

- one request per connection -- the server **closes** after replying
- `session.snapshot` returns `{"result": {"snapshot": {"panes": [...]}}}`

The pane statuses are read from a JSON file on every request, so a test mutates that
file to simulate an agent starting and stopping work. Stopping the server simulates the
Herdr server dying, which is how the daemon is expected to notice it should release.
"""

import json
import os
import socket
import threading


class FakeHerdrServer(object):
    def __init__(self, socket_path, statuses_path):
        self.socket_path = socket_path
        self.statuses_path = statuses_path
        self.sock = None
        self.thread = None
        self.running = False
        self.requests = []

    def set_statuses(self, statuses):
        tmp = self.statuses_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(statuses, fh)
        os.replace(tmp, self.statuses_path)

    def _statuses(self):
        try:
            with open(self.statuses_path, "r") as fh:
                return json.load(fh)
        except (IOError, OSError, ValueError):
            return {}

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.socket_path)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if b"\n" not in buf:
                    continue
                req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                self.requests.append(req)
                conn.sendall((json.dumps(self._reply(req)) + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()  # the real server closes after every reply
                except OSError:
                    pass

    def _reply(self, req):
        rid = req.get("id", "x")
        if req.get("method") != "session.snapshot":
            return {"id": rid, "error": {"code": "unknown_method",
                                         "message": req.get("method")}}
        panes = []
        for pane_id, status in sorted(self._statuses().items()):
            panes.append({"pane_id": pane_id,
                          "workspace_id": pane_id.split(":")[0],
                          "agent_status": status,
                          "cwd": "/tmp",
                          "revision": 1})
        return {"id": rid,
                "result": {"type": "session_snapshot",
                           "snapshot": {"version": "0.8.2", "protocol": 20,
                                        "focused_pane_id": panes[0]["pane_id"]
                                        if panes else None,
                                        "panes": panes, "workspaces": [], "tabs": []}}}

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        if self.thread:
            self.thread.join(timeout=2)
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
