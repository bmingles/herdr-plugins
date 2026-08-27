"""A fake Herdr server for the tracker: `session.snapshot` and `pane.read`.

Fidelity that matters, both measured against a real 0.8.2 server:

- one request per connection -- the server **closes** after replying
- `pane.read` requires a `source`, and returns its text at `result.read.text`

The scenario is driven from a JSON file the test rewrites, so focus changes, agent
statuses and screen contents can all be moved around mid-run.
"""

import json
import os
import socket
import threading


class FakeHerdrServer(object):
    def __init__(self, socket_path, scenario_path):
        self.socket_path = socket_path
        self.scenario_path = scenario_path
        self.sock = None
        self.thread = None
        self.running = False

    # -- scenario ----------------------------------------------------------------

    def set(self, focused_workspace="w1", workspaces=None, panes=None, screens=None):
        """`panes` is [{pane_id, workspace_id, agent_status, focused}]."""
        workspaces = workspaces or [{"workspace_id": "w1", "label": "alpha"}]
        panes = panes or [{"pane_id": "w1:p1", "workspace_id": "w1",
                           "agent_status": "unknown", "focused": True}]
        scenario = {"focused_workspace": focused_workspace,
                    "workspaces": workspaces, "panes": panes,
                    "screens": screens or {}}
        tmp = self.scenario_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(scenario, fh)
        os.replace(tmp, self.scenario_path)

    def _scenario(self):
        try:
            with open(self.scenario_path) as fh:
                return json.load(fh)
        except (IOError, OSError, ValueError):
            return {"focused_workspace": None, "workspaces": [], "panes": [],
                    "screens": {}}

    # -- server ------------------------------------------------------------------

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.socket_path)
        self.sock.listen(16)
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
                conn.sendall((json.dumps(self._reply(req)) + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()          # the real server closes after every reply
                except OSError:
                    pass

    def _reply(self, req):
        rid = req.get("id", "x")
        method = req.get("method")
        scenario = self._scenario()

        if method == "session.snapshot":
            focused = scenario["focused_workspace"]
            focused_pane = None
            for pane in scenario["panes"]:
                if pane.get("focused"):
                    focused_pane = pane["pane_id"]
            return {"id": rid, "result": {"type": "session_snapshot", "snapshot": {
                "version": "0.8.2", "protocol": 20,
                "focused_workspace_id": focused,
                "focused_pane_id": focused_pane,
                "workspaces": scenario["workspaces"],
                "panes": [dict(p, cwd=p.get("cwd", "/tmp"), revision=1)
                          for p in scenario["panes"]],
                "tabs": []}}}

        if method == "pane.read":
            params = req.get("params") or {}
            if "source" not in params:
                return {"id": rid, "error": {"code": "invalid_request",
                                             "message": "missing field `source`"}}
            pane_id = params.get("pane_id")
            known = {p["pane_id"] for p in scenario["panes"]}
            if pane_id not in known:
                return {"id": rid, "error": {"code": "pane_not_found",
                                             "message": "pane %s not found" % pane_id}}
            text = scenario["screens"].get(pane_id, "")
            return {"id": rid, "result": {"type": "pane_read", "read": {
                "pane_id": pane_id, "source": params["source"], "format": "text",
                "text": text, "revision": 0, "truncated": False}}}

        return {"id": rid, "error": {"code": "unknown_method", "message": method}}

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
