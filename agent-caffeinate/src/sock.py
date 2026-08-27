"""One-shot requests to the Herdr socket.

Deliberately *not* a subscription client. Two facts from
`docs/herdr-daemon-facts.md` shaped this:

- The server closes the connection after answering any non-subscribe request, so a
  connection cannot be held and reused for polling.
- `pane.agent_status_changed` subscriptions require a concrete, existing `pane_id` --
  `*` and `""` are both rejected -- so there is no session-wide agent-status stream to
  subscribe to in the first place.

A `session.snapshot` over a fresh connection measured **0.35 ms**, which is ~3x cheaper
than spawning the `herdr` CLI and 0.035% of a core at 1 Hz. Polling is both simpler and
more robust than maintaining per-pane subscriptions.

`ServerGone` is the important signal: a plugin-spawned daemon *outlives its server*
(measured twice), so a failed connect is how the daemon learns to release and exit.
"""

import errno
import json
import os
import socket


class ServerGone(Exception):
    """The Herdr server is not reachable: stop what we are holding and exit."""


class ProtocolError(Exception):
    """The server answered, but not with something usable."""


def default_socket_path(env=None):
    if env is None:
        env = os.environ
    return env.get("HERDR_SOCKET_PATH")


def request(socket_path, method, params=None, timeout=5.0):
    """Send one request, return the parsed reply object.

    Raises :class:`ServerGone` when the socket is missing, refused, or closed before a
    complete line arrived, and :class:`ProtocolError` when the reply is not JSON.
    """
    if not socket_path:
        raise ServerGone("no socket path (HERDR_SOCKET_PATH unset)")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(socket_path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ServerGone("%s: %s" % (socket_path, exc))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTCONN):
                raise ServerGone("%s: %s" % (socket_path, exc))
            raise

        payload = json.dumps({"id": "agent-caffeinate",
                              "method": method,
                              "params": params or {}})
        try:
            sock.sendall((payload + "\n").encode("utf-8"))
        except OSError as exc:
            raise ServerGone("send failed: %s" % exc)

        buf = b""
        while b"\n" not in buf:
            try:
                chunk = sock.recv(1 << 20)
            except socket.timeout:
                raise ServerGone("timed out awaiting a reply to %s" % method)
            except OSError as exc:
                raise ServerGone("recv failed: %s" % exc)
            if not chunk:
                raise ServerGone("server closed before replying to %s" % method)
            buf += chunk
    finally:
        try:
            sock.close()
        except OSError:
            pass

    line = buf.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError("unparseable reply to %s: %s" % (method, exc))


def pane_statuses(socket_path, timeout=5.0):
    """`{pane_id: agent_status}` for every pane in the session.

    Panes with no agent report `"unknown"`, which is simply never in `activeStatuses`.
    """
    reply = request(socket_path, "session.snapshot", timeout=timeout)
    if "error" in reply:
        raise ProtocolError("session.snapshot failed: %s" % json.dumps(reply["error"]))
    try:
        panes = reply["result"]["snapshot"]["panes"]
    except (KeyError, TypeError):
        raise ProtocolError("session.snapshot reply has no result.snapshot.panes")
    out = {}
    for pane in panes:
        pane_id = pane.get("pane_id")
        if pane_id:
            out[pane_id] = pane.get("agent_status", "unknown")
    return out
