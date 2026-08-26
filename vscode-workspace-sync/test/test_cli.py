"""End-to-end tests through the `bin/sync` shim, as Herdr invokes it.

These cover the plan's offline validation items so they stay covered: they run the real
shim as a subprocess with a hand-built environment, never touching a real Herdr session
or a real workspace file.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import jsonc  # noqa: E402

SYNC = os.path.join(_support.PLUGIN_ROOT, "bin", "sync")
PORTABLE = _support.fixture_path("snapshot-portable.json")
EMPTY_SNAPSHOT = _support.fixture_path("snapshot-empty.json")


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vws-cli-")
        self.cfg_dir = os.path.join(self.tmp, "config")
        os.makedirs(self.cfg_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scratch(self, fixture_name="example.code-workspace"):
        dest = os.path.join(self.tmp, fixture_name)
        shutil.copy(_support.fixture_path(fixture_name), dest)
        return dest

    def write_config(self, body):
        with open(os.path.join(self.cfg_dir, "config.json"), "w") as fh:
            fh.write(json.dumps(body, indent=2))

    def env(self, snapshot=PORTABLE, extra=None, with_path=True):
        """A deliberately minimal environment -- no `PATH` unless asked for.

        The server's `PATH` is whatever launched the server and is therefore unknowable,
        which is the whole reason `bin/sync` names the interpreter by absolute path.
        """
        env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "HERDR_PLUGIN_CONFIG_DIR": self.cfg_dir,
        }
        if snapshot:
            env["HERDR_VSCODE_SYNC_FAKE_SNAPSHOT"] = snapshot
        if with_path:
            env["PATH"] = os.environ.get("PATH", "")
        env.update(extra or {})
        return env

    def run_sync(self, args=(), **kwargs):
        proc = subprocess.run(
            [SYNC] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(**kwargs),
            cwd=_support.PLUGIN_ROOT,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8"),
            proc.stderr.decode("utf-8"),
        )


class TestShim(CliCase):
    def test_shim_is_executable_and_mode_755(self):
        self.assertEqual(os.stat(SYNC).st_mode & 0o777, 0o755)

    def test_py_files_are_not_executable_and_have_no_shebang(self):
        for name in sorted(os.listdir(_support.SRC)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(_support.SRC, name)
            self.assertEqual(os.stat(path).st_mode & 0o111, 0, name)
            with open(path) as fh:
                self.assertFalse(fh.read(2) == "#!", name)

    def test_runs_with_no_path_at_all(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        code, out, errout = self.run_sync(["--doctor"], with_path=False)
        self.assertEqual(code, 0, errout)
        self.assertIn("computed folders", out)

    def test_falls_back_to_exit_127_when_no_interpreter_is_found(self):
        fake_root = os.path.join(self.tmp, "fake-plugin")
        os.makedirs(os.path.join(fake_root, "bin"))
        os.symlink(_support.SRC, os.path.join(fake_root, "src"))
        with open(SYNC) as fh:
            shim = fh.read()
        shim = shim.replace(
            "/usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3",
            "/nonexistent/a/python3 /nonexistent/b/python3 /nonexistent/c/python3",
        )
        fake_sync = os.path.join(fake_root, "bin", "sync")
        with open(fake_sync, "w") as fh:
            fh.write(shim)
        os.chmod(fake_sync, 0o755)
        proc = subprocess.run(
            [fake_sync, "--doctor"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # PATH set but empty: an *unset* PATH makes `command -v` fall back to
            # the shell's built-in default (which finds /usr/bin/python3), so the
            # fallback branch has to be starved explicitly.
            env={"HOME": os.environ.get("HOME", "/tmp"), "PATH": ""},
        )
        self.assertEqual(proc.returncode, 127)
        self.assertIn("no python3 found", proc.stderr.decode("utf-8"))
        self.assertIn("xcode-select --install", proc.stderr.decode("utf-8"))


class TestSyncRun(CliCase):
    def test_rewrites_then_reports_unchanged(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})

        code, out, errout = self.run_sync(["--reason", "action"])
        self.assertEqual(code, 0, errout)
        self.assertIn("result=wrote", out)
        self.assertIn("reason=action mode=mirror", out)
        with open(target) as fh:
            parsed = jsonc.loads(fh.read())
        self.assertEqual(
            parsed["folders"],
            [{"path": "/usr/share"}, {"path": "/usr/lib", "name": "the-libs"}],
        )
        # Everything outside `folders` survived.
        self.assertEqual(parsed["settings"]["workbench.colorCustomizations"]
                         ["statusBar.background"], "#1f8d13")
        with open(target) as fh:
            self.assertIn("#1f8d13,", fh.read().replace('"#1f8d13",', "#1f8d13,"))

        code, out, errout = self.run_sync()
        self.assertEqual(code, 0, errout)
        self.assertIn("result=unchanged", out)

    def test_active_mode_unpinned_warns_but_proceeds(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target, "mode": "active"})
        code, out, errout = self.run_sync()
        self.assertEqual(code, 0, errout)
        self.assertIn("extension host", errout)
        self.assertIn("result=wrote", out)

    def test_empty_computed_list_writes_nothing_and_exits_zero(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        with open(target) as fh:
            before = fh.read()
        code, out, errout = self.run_sync(snapshot=EMPTY_SNAPSHOT)
        self.assertEqual(code, 0, errout)
        self.assertIn("result=skipped-empty", out)
        self.assertIn("would blank the VS Code explorer", errout)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)

    def test_missing_target_file_exits_non_zero_and_names_the_path(self):
        missing = os.path.join(self.tmp, "not-there.code-workspace")
        self.write_config({"workspaceFile": missing})
        code, out, errout = self.run_sync()
        self.assertNotEqual(code, 0)
        self.assertIn(missing, errout)
        self.assertFalse(os.path.exists(missing))

    def test_named_session_socket_skips_without_writing(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        with open(target) as fh:
            before = fh.read()
        code, out, errout = self.run_sync(
            extra={"HERDR_SOCKET_PATH": "/x/.config/herdr/sessions/other/herdr.sock"}
        )
        self.assertEqual(code, 0, errout)
        self.assertIn("result=skipped-session", out)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)

    def test_startup_invocation_with_no_event_json(self):
        # `HERDR_PLUGIN_EVENT` is the literal string `startup` and
        # `HERDR_PLUGIN_EVENT_JSON` is unset -- nothing may parse it unconditionally.
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        code, out, errout = self.run_sync(
            ["--reason", "startup"], extra={"HERDR_PLUGIN_EVENT": "startup"}
        )
        self.assertEqual(code, 0, errout)
        self.assertIn("reason=startup", out)
        self.assertIn("result=wrote", out)

    def test_plugin_context_workspace_cwd_is_used_for_the_event_subject(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        context = json.dumps({"workspace_id": "w1", "workspace_cwd": "/usr/bin",
                              "invocation_source": "api",
                              "correlation_id": "workspace.created"})
        code, out, errout = self.run_sync(
            ["--reason", "event"],
            extra={"HERDR_PLUGIN_EVENT": "workspace.created",
                   "HERDR_PLUGIN_EVENT_JSON": '{"event":"workspace_created","data":{}}',
                   "HERDR_PLUGIN_CONTEXT_JSON": context},
        )
        self.assertEqual(code, 0, errout)
        with open(target) as fh:
            # w1's path now comes from context, so its label "share" no longer matches
            # the basename ("bin") and a `name` appears; w3 is no longer a duplicate of
            # w1 and survives.
            self.assertEqual(jsonc.loads(fh.read())["folders"],
                             [{"path": "/usr/bin", "name": "share"},
                              {"path": "/usr/lib", "name": "the-libs"},
                              {"path": "/usr/share"}])

    def test_relative_path_workspace_file_reports_unchanged(self):
        # As VS Code itself writes it: relative paths, one property per line.
        target = os.path.join(self.tmp, "vscode-written.code-workspace")
        shutil.copy(_support.fixture_path("vscode-written.code-workspace"), target)
        for name in ("alpha", "beta", "gamma"):
            os.makedirs(os.path.join(self.tmp, "spaces", name))
        snapshot = os.path.join(self.tmp, "snap.json")
        with open(_support.fixture_path("snapshot-portable.json")) as fh:
            doc = json.load(fh)
        snap = doc["result"]["snapshot"]
        snap["workspaces"] = [
            {"workspace_id": "w1", "label": "alpha", "number": 1,
             "active_tab_id": "w1:t1", "focused": False, "agent_status": "unknown"},
            {"workspace_id": "w2", "label": "GAMMA-RENAMED", "number": 2,
             "active_tab_id": "w2:t1", "focused": True, "agent_status": "unknown"},
            {"workspace_id": "w3", "label": "beta", "number": 3,
             "active_tab_id": "w3:t1", "focused": False, "agent_status": "unknown"},
        ]
        snap["panes"] = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
             "cwd": os.path.join(self.tmp, "spaces", "alpha")},
            {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1",
             "cwd": os.path.join(self.tmp, "spaces", "gamma")},
            {"pane_id": "w3:p1", "workspace_id": "w3", "tab_id": "w3:t1",
             "cwd": os.path.join(self.tmp, "spaces", "beta")},
        ]
        snap["focused_workspace_id"] = "w2"
        with open(snapshot, "w") as fh:
            json.dump(doc, fh)
        self.write_config({"workspaceFile": target})
        with open(target) as fh:
            before = fh.read()
        code, out, errout = self.run_sync(snapshot=snapshot)
        self.assertEqual(code, 0, errout)
        self.assertIn("result=unchanged", out)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)


class TestDoctor(CliCase):
    def test_writes_nothing_and_prints_the_computed_list(self):
        target = self.scratch()
        self.write_config({"workspaceFile": target})
        with open(target) as fh:
            before = fh.read()
        code, out, errout = self.run_sync(["--doctor"])
        self.assertEqual(code, 0, errout)
        for expected in ("config file", "workspaceFile", "target file",
                         "snapshot source", "herdr version", "focused space",
                         "computed folders", "/usr/lib", "would write"):
            self.assertIn(expected, out)
        self.assertIn("result=doctor", out)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)
