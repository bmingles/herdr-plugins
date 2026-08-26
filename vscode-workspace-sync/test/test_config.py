import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import config  # noqa: E402


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vws-cfg-")
        self.target = os.path.join(self.tmp, "w.code-workspace")
        with open(self.target, "w") as fh:
            fh.write("{}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, text):
        cfg_dir = os.path.join(self.tmp, "cfgdir")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
            fh.write(text)
        return cfg_dir

    def load(self, raw=None, text=None, env_extra=None):
        if text is None:
            body = dict(raw or {})
            body.setdefault("workspaceFile", self.target)
            text = json.dumps(body)
        cfg_dir = self.write_config(text)
        env = {"HERDR_PLUGIN_CONFIG_DIR": cfg_dir}
        env.update(env_extra or {})
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            return config.load(env)
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved


class TestLoad(ConfigCase):
    def test_defaults(self):
        cfg = self.load({})
        self.assertEqual(cfg.mode, "mirror")
        self.assertEqual(cfg.pinned_folders, [])

    def test_comments_and_trailing_commas_allowed(self):
        text = '{\n  // a comment\n  "workspaceFile": "%s",\n}\n' % self.target
        cfg = self.load(text=text)
        self.assertEqual(cfg.workspace_file, self.target)

    def test_tilde_expansion(self):
        cfg = self.load({"workspaceFile": "~/w.code-workspace",
                         "pinnedFolders": ["~/pinned"]})
        home = os.path.expanduser("~")
        self.assertEqual(cfg.workspace_file, os.path.join(home, "w.code-workspace"))
        self.assertEqual(cfg.pinned_folders, [os.path.join(home, "pinned")])

    def test_env_override_wins(self):
        other = os.path.join(self.tmp, "other.code-workspace")
        cfg = self.load({"workspaceFile": self.target},
                        env_extra={config.ENV_WORKSPACE_FILE: other})
        self.assertEqual(cfg.workspace_file, other)

    def test_unknown_key_is_a_warning_not_an_error(self):
        cfg = self.load({"nope": 1, "alsoNope": 2})
        self.assertEqual(len(cfg.warnings), 2)
        self.assertTrue(any("nope" in w for w in cfg.warnings))

    def test_missing_workspace_file_is_fatal_and_names_the_config_path(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load({"workspaceFile": None})
        message = str(ctx.exception)
        self.assertIn("config.json", message)
        self.assertIn("herdr plugin config-dir vscode-workspace-sync", message)

    def test_missing_config_file_is_fatal_and_names_the_config_path(self):
        cfg_dir = os.path.join(self.tmp, "empty-cfgdir")
        os.makedirs(cfg_dir)
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            with self.assertRaises(config.ConfigError) as ctx:
                config.load({"HERDR_PLUGIN_CONFIG_DIR": cfg_dir})
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved
        self.assertIn(os.path.join(cfg_dir, "config.json"), str(ctx.exception))

    def test_env_override_makes_a_missing_config_file_acceptable(self):
        cfg_dir = os.path.join(self.tmp, "empty2")
        os.makedirs(cfg_dir)
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            cfg = config.load({"HERDR_PLUGIN_CONFIG_DIR": cfg_dir,
                               config.ENV_WORKSPACE_FILE: self.target})
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved
        self.assertEqual(cfg.workspace_file, self.target)

    def test_bad_json_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load(text="{ not json")

    def test_bad_mode_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load({"mode": "sideways"})

    def test_active_mode_accepted_with_no_pinned_folders(self):
        # Never a validation error: unpinned `active` is supported, just costlier.
        cfg = self.load({"mode": "active"})
        self.assertEqual(cfg.mode, "active")
        self.assertEqual(cfg.pinned_folders, [])



if __name__ == "__main__":
    unittest.main()
