"""Tests for the inbound direction (`src/adopt.py` and the `bin/adopt` shim).

The pure planning layer is tested directly; everything that would touch a real Herdr
session goes through `test/fake-herdr` via `HERDR_BIN_PATH`, so these tests assert on
the exact argv adopt would send. That matters more here than in the sync direction:
measured on herdr 0.8.2, a `workspace create --cwd` that is relative or names a missing
directory **succeeds** and silently roots the Space at `$HOME`, so "adopt passed an
absolute, existing path" is a correctness property with no downstream check.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import adopt  # noqa: E402
import config  # noqa: E402
from herdr import Space  # noqa: E402

ADOPT = os.path.join(_support.PLUGIN_ROOT, "bin", "adopt")
FAKE_HERDR = os.path.join(_support.TEST_DIR, "fake-herdr")
PORTABLE = _support.fixture_path("snapshot-portable.json")
EMPTY_SNAPSHOT = _support.fixture_path("snapshot-empty.json")
SPACES = _support.fixture_path("spaces")


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vws-adopt-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DiscoverTest(TempCase):
    def test_no_workspace_file_is_an_error(self):
        with self.assertRaises(adopt.AdoptError) as cm:
            adopt.discover_workspace_file(self.tmp)
        self.assertIn("no *.code-workspace", str(cm.exception))

    def test_exactly_one_is_returned(self):
        path = self.write("only.code-workspace", "{}")
        self.assertEqual(adopt.discover_workspace_file(self.tmp), path)

    def test_several_refuses_and_names_them(self):
        self.write("a.code-workspace", "{}")
        self.write("b.code-workspace", "{}")
        with self.assertRaises(adopt.AdoptError) as cm:
            adopt.discover_workspace_file(self.tmp)
        message = str(cm.exception)
        self.assertIn("2 *.code-workspace files", message)
        self.assertIn("a.code-workspace", message)
        self.assertIn("b.code-workspace", message)
        self.assertIn("--file", message)

    def test_a_directory_named_like_one_is_not_a_match(self):
        os.mkdir(os.path.join(self.tmp, "trap.code-workspace"))
        with self.assertRaises(adopt.AdoptError):
            adopt.discover_workspace_file(self.tmp)


class DiscoveryDirTest(unittest.TestCase):
    def test_plain_run_uses_the_process_cwd(self):
        self.assertEqual(adopt.discovery_dir({}), os.getcwd())

    def test_action_uses_the_focused_pane_cwd_not_the_plugin_root(self):
        env = {
            "HERDR_PLUGIN_ACTION_ID": "adopt",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                {"focused_pane_cwd": "/usr/lib", "workspace_cwd": "/usr/share"}
            ),
        }
        self.assertEqual(adopt.discovery_dir(env), "/usr/lib")

    def test_action_falls_back_to_the_workspace_cwd(self):
        env = {
            "HERDR_PLUGIN_ACTION_ID": "adopt",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_cwd": "/usr/share"}),
        }
        self.assertEqual(adopt.discovery_dir(env), "/usr/share")

    def test_action_without_any_context_cwd_is_an_error(self):
        env = {"HERDR_PLUGIN_ACTION_ID": "adopt", "HERDR_PLUGIN_CONTEXT_JSON": "{}"}
        with self.assertRaises(adopt.AdoptError):
            adopt.discovery_dir(env)


# ---------------------------------------------------------------------------
# Reading the workspace file
# ---------------------------------------------------------------------------


class ReadFoldersTest(TempCase):
    def test_basic_jsonc_with_comments_and_trailing_comma(self):
        refs, warnings = adopt.read_folders(
            _support.fixture_path("adopt-basic.code-workspace")
        )
        self.assertEqual([r.path for r in refs], ["/tmp", "/usr", "/etc"])
        self.assertEqual([r.name for r in refs], [None, "system", None])
        self.assertEqual(warnings, [])

    def test_relative_paths_resolve_against_the_files_directory(self):
        fixture = _support.fixture_path("adopt-relative.code-workspace")
        base = os.path.dirname(fixture)
        refs, warnings = adopt.read_folders(fixture)
        # "spaces" and "./spaces" collapse to one entry; ".." is the parent.
        self.assertEqual(
            [r.path for r in refs],
            [os.path.join(base, "spaces"), os.path.dirname(base)],
        )
        self.assertTrue(any("duplicates" in w for w in warnings))

    def test_relative_resolution_ignores_the_process_cwd(self):
        """The whole point: $PWD must not influence the result."""
        fixture = _support.fixture_path("adopt-relative.code-workspace")
        first, _ = adopt.read_folders(fixture)
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            second, _ = adopt.read_folders(fixture)
        finally:
            os.chdir(cwd)
        self.assertEqual([r.path for r in first], [r.path for r in second])

    def test_malformed_entries_warn_and_are_dropped(self):
        refs, warnings = adopt.read_folders(
            _support.fixture_path("adopt-messy.code-workspace")
        )
        self.assertEqual([r.path for r in refs], ["/tmp", "/usr"])
        # An empty "name" is treated as absent, not as a label.
        self.assertIsNone(refs[1].name)
        joined = "\n".join(warnings)
        self.assertIn("duplicates", joined)
        self.assertIn("no string \"path\"", joined)
        self.assertIn("not an object", joined)
        self.assertIn("${...}", joined)

    def test_variable_substitution_is_declined_not_guessed(self):
        _refs, warnings = adopt.read_folders(
            _support.fixture_path("adopt-messy.code-workspace")
        )
        self.assertTrue(
            any("${workspaceFolder}/sub" in w and "ignored" in w for w in warnings)
        )

    def test_missing_folders_key_is_fatal(self):
        with self.assertRaises(adopt.AdoptError) as cm:
            adopt.read_folders(_support.fixture_path("adopt-no-folders.code-workspace"))
        self.assertIn("no top-level \"folders\"", str(cm.exception))

    def test_folders_must_be_an_array(self):
        with self.assertRaises(adopt.AdoptError) as cm:
            adopt.read_folders(
                _support.fixture_path("adopt-folders-not-array.code-workspace")
            )
        self.assertIn("must be an array", str(cm.exception))

    def test_invalid_json_is_fatal(self):
        path = self.write("bad.code-workspace", "{ not json")
        with self.assertRaises(adopt.AdoptError) as cm:
            adopt.read_folders(path)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_tilde_is_expanded(self):
        path = self.write(
            "tilde.code-workspace", json.dumps({"folders": [{"path": "~/somewhere"}]})
        )
        refs, _ = adopt.read_folders(path)
        self.assertEqual(refs[0].path, os.path.join(os.path.expanduser("~"), "somewhere"))


class ResolveFolderPathTest(unittest.TestCase):
    def test_absolute_is_normalised_but_not_symlink_resolved(self):
        # realpath would rewrite /tmp to /private/tmp on macOS; resolve_path must not.
        self.assertEqual(adopt.resolve_folder_path("/tmp/", "/base"), "/tmp")
        self.assertEqual(adopt.resolve_folder_path("/a/b/../c", "/base"), "/a/c")

    def test_relative_joins_the_base_dir(self):
        self.assertEqual(adopt.resolve_folder_path("sub/dir", "/base"), "/base/sub/dir")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def ref(path, name=None):
    return adopt.FolderRef(path, name)


class PlanTest(unittest.TestCase):
    def test_missing_space_is_a_create(self):
        plan = adopt.plan_adoption([ref("/usr/bin")], [], isdir=lambda p: True)
        self.assertEqual([i.action for i in plan.items], [adopt.ACTION_CREATE])

    def test_existing_path_is_never_recreated(self):
        """Herdr does not dedupe by path, so this check is the only thing preventing
        a second Space on every re-run."""
        spaces = [Space("w1", "libs", "/usr/lib")]
        plan = adopt.plan_adoption([ref("/usr/lib")], spaces, isdir=lambda p: True)
        self.assertEqual([i.action for i in plan.items], [adopt.ACTION_EXISTS])
        self.assertEqual(plan.items[0].space_id, "w1")
        self.assertEqual(plan.of(adopt.ACTION_CREATE), [])

    def test_existing_match_ignores_a_trailing_slash(self):
        spaces = [Space("w1", "libs", "/usr/lib/")]
        plan = adopt.plan_adoption([ref("/usr/lib")], spaces, isdir=lambda p: True)
        self.assertEqual([i.action for i in plan.items], [adopt.ACTION_EXISTS])

    def test_nonexistent_directory_is_skipped_not_created(self):
        plan = adopt.plan_adoption([ref("/nope")], [], isdir=lambda p: False)
        self.assertEqual([i.action for i in plan.items], [adopt.ACTION_SKIP])
        self.assertEqual(plan.of(adopt.ACTION_CREATE), [])

    def test_two_spaces_on_one_path_still_count_as_covered(self):
        spaces = [Space("w1", "share", "/usr/share"), Space("w3", "share", "/usr/share")]
        plan = adopt.plan_adoption([ref("/usr/share")], spaces, isdir=lambda p: True)
        self.assertEqual(plan.items[0].action, adopt.ACTION_EXISTS)
        self.assertEqual(plan.items[0].space_id, "w1")

    def test_space_with_no_cwd_is_ignored_for_matching(self):
        plan = adopt.plan_adoption(
            [ref("/usr/lib")], [Space("w1", "x", None)], isdir=lambda p: True
        )
        self.assertEqual(plan.items[0].action, adopt.ACTION_CREATE)

    def test_order_follows_the_file(self):
        refs = [ref("/a"), ref("/b"), ref("/c")]
        plan = adopt.plan_adoption(refs, [], isdir=lambda p: True)
        self.assertEqual([i.ref.path for i in plan.items], ["/a", "/b", "/c"])

    def test_extras_are_reported_and_left_alone(self):
        spaces = [Space("w1", "libs", "/usr/lib"), Space("w2", "other", "/opt")]
        plan = adopt.plan_adoption([ref("/usr/lib")], spaces, isdir=lambda p: True)
        self.assertEqual([s.id for s in plan.extras], ["w2"])

    def test_relabel_is_off_by_default(self):
        spaces = [Space("w1", "old", "/usr/lib")]
        plan = adopt.plan_adoption([ref("/usr/lib", "new")], spaces, isdir=lambda p: True)
        self.assertEqual(plan.renames, [])

    def test_relabel_queues_a_rename_when_the_name_differs(self):
        spaces = [Space("w1", "old", "/usr/lib")]
        plan = adopt.plan_adoption(
            [ref("/usr/lib", "new")], spaces, relabel=True, isdir=lambda p: True
        )
        self.assertEqual(len(plan.renames), 1)
        self.assertEqual(plan.renames[0].space_id, "w1")
        self.assertEqual(plan.renames[0].new_label, "new")

    def test_relabel_is_a_no_op_when_the_label_already_matches(self):
        spaces = [Space("w1", "same", "/usr/lib")]
        plan = adopt.plan_adoption(
            [ref("/usr/lib", "same")], spaces, relabel=True, isdir=lambda p: True
        )
        self.assertEqual(plan.renames, [])

    def test_relabel_never_fires_for_an_entry_with_no_name(self):
        """Herdr derives the label from the basename, so "no name" means "no opinion"."""
        spaces = [Space("w1", "custom", "/usr/lib")]
        plan = adopt.plan_adoption(
            [ref("/usr/lib")], spaces, relabel=True, isdir=lambda p: True
        )
        self.assertEqual(plan.renames, [])


# ---------------------------------------------------------------------------
# The mutual-exclusivity guard
# ---------------------------------------------------------------------------


class GuardTest(TempCase):
    def setUp(self):
        TempCase.setUp(self)
        self.cfg_dir = os.path.join(self.tmp, "config")
        os.makedirs(self.cfg_dir)
        self._saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.cfg_dir

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        else:
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self._saved
        TempCase.tearDown(self)

    def write_config(self, body):
        with open(os.path.join(self.cfg_dir, "config.json"), "w") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))

    def test_no_config_file_at_all_allows_adopt(self):
        self.assertIsNone(adopt.guard({}))

    def test_sync_managed_default_session_is_refused(self):
        self.write_config({"workspaceFile": "/tmp/x.code-workspace"})
        refusal = adopt.guard({})
        self.assertIsNotNone(refusal)
        self.assertIn("mutually exclusive", refusal)
        self.assertIn("/tmp/x.code-workspace", refusal)

    def test_session_mapped_in_the_sessions_map_is_refused(self):
        self.write_config({"sessions": {"work": {"workspaceFile": "/tmp/w.code-workspace"}}})
        refusal = adopt.guard(
            {"HERDR_SOCKET_PATH": "/h/.config/herdr/sessions/work/herdr.sock"}
        )
        self.assertIsNotNone(refusal)
        self.assertIn("'work'", refusal)

    def test_unmapped_session_allows_adopt(self):
        """Rule 4 -- the session syncs nothing, so it is the adoptable one."""
        self.write_config({"sessions": {"work": {"workspaceFile": "/tmp/w.code-workspace"}}})
        self.assertIsNone(
            adopt.guard({"HERDR_SOCKET_PATH": "/h/.config/herdr/sessions/oss/herdr.sock"})
        )

    def test_named_session_with_only_a_top_level_file_allows_adopt(self):
        """Rule 3 reaches only the default session, so a named one stays adoptable."""
        self.write_config({"workspaceFile": "/tmp/x.code-workspace"})
        self.assertIsNone(
            adopt.guard({"HERDR_SOCKET_PATH": "/h/.config/herdr/sessions/probe/herdr.sock"})
        )

    def test_env_override_is_refused(self):
        refusal = adopt.guard({config.ENV_WORKSPACE_FILE: "/tmp/x.code-workspace"})
        self.assertIsNotNone(refusal)
        self.assertIn(config.ENV_WORKSPACE_FILE, refusal)

    def test_broken_config_raises_rather_than_silently_allowing(self):
        self.write_config("{ this is not json")
        with self.assertRaises(config.ConfigError):
            adopt.guard({})


# ---------------------------------------------------------------------------
# End to end, through the bin/adopt shim
# ---------------------------------------------------------------------------


class CliTest(TempCase):
    def setUp(self):
        TempCase.setUp(self)
        self.cfg_dir = os.path.join(self.tmp, "config")
        os.makedirs(self.cfg_dir)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.calls = os.path.join(self.tmp, "calls.jsonl")

    def env(self, extra=None, snapshot=EMPTY_SNAPSHOT):
        env = {
            "HOME": self.tmp,
            "PATH": os.environ.get("PATH", ""),
            "HERDR_PLUGIN_CONFIG_DIR": self.cfg_dir,
            "HERDR_BIN_PATH": FAKE_HERDR,
            "FAKE_HERDR_LOG": self.calls,
        }
        if snapshot:
            env["HERDR_VSCODE_SYNC_FAKE_SNAPSHOT"] = snapshot
        env.update(extra or {})
        return env

    def run_adopt(self, args, extra=None, cwd=None, snapshot=EMPTY_SNAPSHOT):
        return subprocess.run(
            [ADOPT] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or self.work,
            env=self.env(extra, snapshot),
        )

    def herdr_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def workspace_file(self, folders, name="test.code-workspace"):
        path = os.path.join(self.work, name)
        with open(path, "w") as fh:
            fh.write(json.dumps({"folders": folders}, indent=2))
        return path

    # -- guard --------------------------------------------------------------

    def test_refuses_in_a_sync_managed_session_with_exit_2(self):
        with open(os.path.join(self.cfg_dir, "config.json"), "w") as fh:
            fh.write(json.dumps({"workspaceFile": "/tmp/x.code-workspace"}))
        self.workspace_file([{"path": SPACES + "/alpha"}])
        proc = self.run_adopt(["--dry-run"])
        self.assertEqual(proc.returncode, adopt.EXIT_REFUSED)
        self.assertIn(b"mutually exclusive", proc.stderr)
        self.assertEqual(self.herdr_calls(), [])

    def test_broken_config_exits_1_not_2(self):
        with open(os.path.join(self.cfg_dir, "config.json"), "w") as fh:
            fh.write("{ nope")
        proc = self.run_adopt(["--dry-run"])
        self.assertEqual(proc.returncode, adopt.EXIT_FAIL)

    # -- discovery ----------------------------------------------------------

    def test_no_workspace_file_exits_1(self):
        proc = self.run_adopt([])
        self.assertEqual(proc.returncode, adopt.EXIT_FAIL)
        self.assertIn(b"no *.code-workspace", proc.stderr)

    def test_two_workspace_files_exits_1(self):
        self.workspace_file([], "a.code-workspace")
        self.workspace_file([], "b.code-workspace")
        proc = self.run_adopt([])
        self.assertEqual(proc.returncode, adopt.EXIT_FAIL)
        self.assertIn(b"Pick one with --file", proc.stderr)

    def test_discovery_finds_the_single_file_in_cwd(self):
        self.workspace_file([{"path": SPACES + "/alpha"}])
        proc = self.run_adopt(["--dry-run"])
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        self.assertIn(b"result=dry-run", proc.stdout)

    # -- dry run ------------------------------------------------------------

    def test_dry_run_creates_nothing(self):
        self.workspace_file([{"path": SPACES + "/alpha"}, {"path": SPACES + "/beta"}])
        proc = self.run_adopt(["--dry-run"])
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        self.assertIn(b"--dry-run: nothing was created", proc.stdout)
        self.assertIn(b"created=0", proc.stdout)
        self.assertEqual(
            [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]], []
        )

    # -- creating -----------------------------------------------------------

    def test_creates_one_space_per_folder_in_file_order(self):
        self.workspace_file([
            {"path": SPACES + "/alpha"},
            {"path": SPACES + "/beta", "name": "the-beta"},
            {"path": SPACES + "/gamma"},
        ])
        proc = self.run_adopt([])
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        creates = [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]]
        self.assertEqual(len(creates), 3)
        self.assertEqual(
            [c[c.index("--cwd") + 1] for c in creates],
            [SPACES + "/alpha", SPACES + "/beta", SPACES + "/gamma"],
        )
        self.assertIn(b"created=3", proc.stdout)

    def test_every_cwd_passed_to_herdr_is_absolute_and_exists(self):
        """The defence against the two silent footguns measured on herdr 0.8.2."""
        self.workspace_file([{"path": "alpha"}, {"path": "beta"}],)
        # Relative to the workspace file, which lives in self.work -- so make them real.
        os.makedirs(os.path.join(self.work, "alpha"))
        os.makedirs(os.path.join(self.work, "beta"))
        proc = self.run_adopt([])
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        for call in self.herdr_calls():
            if call[:2] != ["workspace", "create"]:
                continue
            cwd = call[call.index("--cwd") + 1]
            self.assertTrue(os.path.isabs(cwd), cwd)
            self.assertTrue(os.path.isdir(cwd), cwd)

    def test_no_focus_is_always_passed(self):
        self.workspace_file([{"path": SPACES + "/alpha"}])
        self.run_adopt([])
        creates = [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]]
        self.assertTrue(all("--no-focus" in c for c in creates))

    def test_label_is_only_passed_when_the_file_names_one(self):
        self.workspace_file([
            {"path": SPACES + "/alpha"},
            {"path": SPACES + "/beta", "name": "custom"},
        ])
        self.run_adopt([])
        creates = [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]]
        self.assertNotIn("--label", creates[0])
        self.assertEqual(creates[1][creates[1].index("--label") + 1], "custom")

    def test_nonexistent_folder_is_skipped_and_never_reaches_herdr(self):
        self.workspace_file([
            {"path": SPACES + "/alpha"},
            {"path": "/definitely/not/here"},
        ])
        proc = self.run_adopt([])
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        self.assertIn(b"skipped=1", proc.stdout)
        creates = [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertNotIn("/definitely/not/here", " ".join(creates[0]))

    def test_rerun_against_existing_spaces_creates_nothing(self):
        """The no-dedupe defence: /usr/share and /usr/lib are already Spaces in the
        portable snapshot, so a file naming them must be a complete no-op."""
        self.workspace_file([{"path": "/usr/share"}, {"path": "/usr/lib"}])
        proc = self.run_adopt([], snapshot=PORTABLE)
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        self.assertIn(b"result=nothing-to-do", proc.stdout)
        self.assertIn(b"existing=2", proc.stdout)
        self.assertEqual(
            [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]], []
        )

    def test_extras_are_listed_but_untouched(self):
        self.workspace_file([{"path": "/usr/lib"}])
        proc = self.run_adopt([], snapshot=PORTABLE)
        self.assertIn(b"Spaces not in the workspace file", proc.stdout)
        self.assertIn(b"w1", proc.stdout)
        self.assertEqual(
            [c for c in self.herdr_calls() if c[:2] == ["workspace", "close"]], []
        )

    def test_a_failed_create_does_not_block_the_rest_but_fails_the_run(self):
        self.workspace_file([
            {"path": SPACES + "/alpha"},
            {"path": SPACES + "/beta"},
            {"path": SPACES + "/gamma"},
        ])
        proc = self.run_adopt([], extra={"FAKE_HERDR_FAIL": "beta"})
        self.assertEqual(proc.returncode, adopt.EXIT_FAIL)
        creates = [c for c in self.herdr_calls() if c[:2] == ["workspace", "create"]]
        self.assertEqual(len(creates), 3)          # all three were attempted
        self.assertIn(b"created=2", proc.stdout)   # two succeeded
        self.assertIn(b"failed to create", proc.stderr)

    # -- relabel ------------------------------------------------------------

    def test_relabel_off_by_default_sends_no_rename(self):
        self.workspace_file([{"path": "/usr/lib", "name": "brand-new"}])
        self.run_adopt([], snapshot=PORTABLE)
        self.assertEqual(
            [c for c in self.herdr_calls() if c[:2] == ["workspace", "rename"]], []
        )

    def test_relabel_renames_the_existing_space(self):
        self.workspace_file([{"path": "/usr/lib", "name": "brand-new"}])
        proc = self.run_adopt(["--relabel"], snapshot=PORTABLE)
        self.assertEqual(proc.returncode, adopt.EXIT_OK, proc.stderr)
        renames = [c for c in self.herdr_calls() if c[:2] == ["workspace", "rename"]]
        self.assertEqual(renames, [["workspace", "rename", "w2", "brand-new"]])
        self.assertIn(b"renamed=1", proc.stdout)

    # -- shape --------------------------------------------------------------

    def test_summary_line_shape(self):
        self.workspace_file([{"path": SPACES + "/alpha"}])
        proc = self.run_adopt(["--dry-run", "--reason", "action"])
        line = [
            l for l in proc.stdout.decode().splitlines()
            if l.startswith(config.PLUGIN_ID + ":")
        ][-1]
        for token in ("reason=action", "session=default", "folders=1", "created=0",
                      "existing=0", "skipped=0", "renamed=0", "result=dry-run"):
            self.assertIn(token, line)


class ShimTest(unittest.TestCase):
    """The shim contract `bin/sync` already relies on, restated for `bin/adopt`.

    Mode bits are read from `os.stat`, not `os.access`: on a bind-mounted working tree
    (Docker Desktop, virtiofs) `os.access(..., X_OK)` returns True for a 0644 file. The
    committed mode is what Herdr actually gets, so that is what is asserted.
    """

    def _executable(self, path):
        return bool(os.stat(path).st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    def test_shim_is_executable(self):
        self.assertTrue(self._executable(ADOPT))

    def test_module_is_not_executable_and_has_no_shebang(self):
        """`src/*.py` are handed to the interpreter as arguments, never exec'd -- a
        shebang would resolve through the server's unknowable PATH."""
        module = os.path.join(_support.SRC, "adopt.py")
        self.assertFalse(self._executable(module))
        with open(module) as fh:
            self.assertFalse(fh.readline().startswith("#!"))

    def test_module_name_does_not_shadow_a_stdlib_module(self):
        """`src` is sys.path[0], so `src/glob.py` would break the first stdlib import."""
        names = getattr(sys, "stdlib_module_names", None)
        if names is None:                       # Python < 3.10
            self.skipTest("sys.stdlib_module_names needs 3.10+")
        for entry in os.listdir(_support.SRC):
            if entry.endswith(".py"):
                self.assertNotIn(entry[:-3], names, entry)

    def test_adopt_is_not_registered_as_an_event_hook(self):
        """Adopt must never be event-driven: a Space is not regenerable."""
        with open(os.path.join(_support.PLUGIN_ROOT, "herdr-plugin.toml")) as fh:
            manifest = fh.read()
        # Strip comments first -- the manifest explains at length why adopt is *not* an
        # event hook, and a naive split would match that prose.
        body = "\n".join(
            line for line in manifest.splitlines() if not line.lstrip().startswith("#")
        )
        for block in body.split("[[")[1:]:
            if block.startswith("events]]"):
                self.assertNotIn("bin/adopt", block)

    def test_adopt_is_registered_as_an_action(self):
        with open(os.path.join(_support.PLUGIN_ROOT, "herdr-plugin.toml")) as fh:
            manifest = fh.read()
        body = "\n".join(
            line for line in manifest.splitlines() if not line.lstrip().startswith("#")
        )
        actions = [b for b in body.split("[[")[1:] if b.startswith("actions]]")]
        self.assertTrue(any("bin/adopt" in b for b in actions))


if __name__ == "__main__":
    unittest.main()
