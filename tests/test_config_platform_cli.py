import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from social_agent.__main__ import main
from social_agent.config import load_api_key, load_config
from social_agent.platform import Action, FakePlatform, PlatformPort
from tests.helpers import make_config, write_log


class ConfigTests(unittest.TestCase):
    def test_dotenv_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_api_key(path), "")
                path.write_text('# 填写密钥\nDEEPSEEK_API_KEY="example-key"\n', encoding="utf-8-sig")
                self.assertEqual(load_api_key(path), "example-key")
                self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}):
                    self.assertEqual(load_api_key(path), "environment-key")
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
                    self.assertEqual(load_api_key(path), "")

    def test_malformed_dotenv_error_does_not_expose_its_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            with patch.dict(os.environ, {}, clear=True):
                for content in ['DEEPSEEK_API_KEY="secret-value', 'DEEPSEEK_API_KEY=first\nDEEPSEEK_API_KEY=second']:
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError) as captured:
                        load_api_key(path)
                    self.assertNotIn("secret-value", str(captured.exception))

    def test_relative_paths_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "agent.toml"
            path.write_text('[agent]\nmodel="configured-model"\ndata_dir="state"\ntimeout_seconds=21\n', encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_MODEL": "environment-model", "USERPROFILE": str(root), "HOME": str(root)}, clear=True), patch("social_agent.config.find_dsh_executable", return_value=None):
                config = load_config(path)
            self.assertEqual(config.model, "environment-model")
            self.assertEqual(config.data_dir, root / "state")
            self.assertEqual(config.timeout_seconds, 21)

    def test_unknown_fields_and_invalid_timeouts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.toml"
            with patch.dict(os.environ, {"USERPROFILE": temporary, "HOME": temporary}, clear=True), patch("social_agent.config.find_dsh_executable", return_value=None):
                for text in ['[agent]\ntimeout_second=1', '[agent]\ntimeout_seconds=-1', '[agent]\ntimeout_seconds=nan', '[agent]\nmodel=""']:
                    with self.subTest(text=text):
                        path.write_text(text, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            load_config(path)


class PlatformTests(unittest.TestCase):
    def test_fake_port_has_stable_observations_and_explicit_simulation(self):
        platform: PlatformPort = FakePlatform()
        first = platform.observe("one")
        self.assertEqual(first, platform.observe("one"))
        self.assertEqual(len(first.posts), 2)
        for action in [Action("browse"), Action("like", first.posts[0].post_id), Action("comment", first.posts[0].post_id, "模拟评论"), Action("post", text="模拟帖子"), Action("skip")]:
            result = platform.execute("one", action)
            self.assertTrue(result.success)
            self.assertTrue(result.simulated)
        self.assertEqual(len(platform.history), 5)

    def test_invalid_actions_are_rejected_before_recording(self):
        platform = FakePlatform()
        for action in [Action("like"), Action("post", text=""), Action("comment", "demo-001", " "), Action("unknown")]:
            with self.assertRaises(ValueError):
                platform.execute("one", action)
        self.assertEqual(platform.history, [])


class CliTests(unittest.TestCase):
    def test_redirected_cli_output_is_utf8_even_with_a_legacy_codepage(self):
        environment = dict(os.environ, PYTHONIOENCODING="ascii")
        process = subprocess.run(
            [sys.executable, "-m", "social_agent", "--help"],
            env=environment, capture_output=True, timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8"))
        self.assertIn("调用本地 DSH", process.stdout.decode("utf-8"))

    def test_doctor_never_prints_api_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            output = io.StringIO()
            with patch("social_agent.__main__.load_config", return_value=config), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "private-example-value"}), redirect_stdout(output):
                self.assertEqual(main(["doctor"]), 1)
            self.assertIn("已设置", output.getvalue())
            self.assertNotIn("private-example-value", output.getvalue())

    def test_export_command_does_not_require_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_log(config.dsh_home, root)
            output = io.StringIO()
            with patch("social_agent.__main__.load_config", return_value=config), patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), redirect_stdout(output):
                code = main(["export", "--session-id", "session-test-001", "--cwd", str(root), "--output-dir", str(root / "export")])
            self.assertEqual(code, 0, output.getvalue())
            self.assertIsNone(json.loads(output.getvalue())["error"])

    def test_missing_prompt_file_is_a_friendly_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            error = io.StringIO()
            with patch("social_agent.__main__.load_config", return_value=config), redirect_stderr(error):
                code = main(["run", "--account", "one", "--prompt-file", str(Path(temporary) / "absent.txt")])
            self.assertEqual(code, 1)
            self.assertIn("运行失败", error.getvalue())
