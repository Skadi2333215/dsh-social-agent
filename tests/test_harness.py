import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from social_agent.harness import HarnessClient
from tests.helpers import FakeHarnessClient, TEST_KEY, make_config


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "中文目录"
        self.cwd.mkdir()
        self.config = make_config(self.root)
        self.client = FakeHarnessClient(self.config)
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": TEST_KEY})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def send_mode(self, mode, **extra):
        return self.client.send(json.dumps({"mode": mode, **extra}), cwd=self.cwd)

    def test_unicode_shell_symbols_and_long_prompt_are_passed_verbatim(self):
        prompt = '中文 `whoami` $(whoami) & | "换行"\n' + "长消息" * 6000
        result = self.client.send(prompt, cwd=self.cwd)
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.reply, "虚拟回复：" + prompt)

    def test_session_callback_runs_before_final_reply(self):
        observed = []

        def remember(identifier):
            # 子进程会继续写入日志；在这里捕获的是收到开头 session 事件的时刻。
            observed.append(identifier)

        result = self.client.send("hello", cwd=self.cwd, on_session=remember)
        self.assertEqual(observed, [result.session_id])
        self.assertTrue(result.succeeded, result.error)

    def test_callback_failure_is_reported_and_output_is_still_drained(self):
        def fail_save(identifier):
            raise OSError("模拟映射写盘失败")

        result = self.client.send("hello", cwd=self.cwd, on_session=fail_save)
        self.assertFalse(result.succeeded)
        self.assertIn("映射写盘失败", result.error)
        self.assertTrue(result.reply)

    def test_large_stderr_does_not_deadlock(self):
        result = self.send_mode("large-stderr")
        self.assertTrue(result.succeeded, result.error)
        self.assertGreater(len(result.stderr), 100_000)

    def test_large_final_reply_is_not_truncated(self):
        result = self.send_mode("large-reply")
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.reply, "中文长文本" * 10_000)

    def test_errors_missing_final_bad_json_and_failed_turn_are_failures(self):
        for mode in ["fail", "no-final", "invalid-json", "missing-session", "wrong-cwd", "failed-turn"]:
            with self.subTest(mode=mode):
                self.assertFalse(self.send_mode(mode).succeeded)

    def test_resume_requires_the_exact_session_id(self):
        first = self.client.send("first", cwd=self.cwd)
        result = self.client.send(
            json.dumps({"mode": "wrong-session"}), cwd=self.cwd, session_id=first.session_id,
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.session_id, first.session_id)
        self.assertIn("不一致", result.error)

    def test_missing_credentials_and_installation_are_clear_failures(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            result = self.client.send("hello", cwd=self.cwd)
            self.assertIn("DEEPSEEK_API_KEY", result.error)
        actual_client = HarnessClient(self.config)
        self.assertIn("app.asar", actual_client.send("hello", cwd=self.cwd).error)
        from dataclasses import replace
        absent_client = HarnessClient(replace(self.config, dsh_executable=self.root / "absent.exe"))
        self.assertIn("入口", absent_client.send("hello", cwd=self.cwd).error)

    def test_known_api_key_is_redacted_from_captured_outputs(self):
        result = self.send_mode("secret")
        self.assertTrue(result.succeeded, result.error)
        self.assertNotIn(TEST_KEY, result.stdout + result.stderr + result.reply)
        self.assertIn("[REDACTED]", result.stderr)

    def test_dotenv_key_is_passed_only_to_child_and_redacted(self):
        env_file = self.root / ".env"
        env_file.write_text(f"DEEPSEEK_API_KEY={TEST_KEY}\n", encoding="utf-8")
        client = FakeHarnessClient(replace(self.config, env_file=env_file))
        with patch.dict(os.environ):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            result = client.send(json.dumps({"mode": "secret"}), cwd=self.cwd)
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertTrue(result.succeeded, result.error)
        self.assertNotIn(TEST_KEY, result.stdout + result.stderr + result.reply)

    def test_timeout_preserves_session_and_terminates_descendants(self):
        client = FakeHarnessClient(make_config(self.root, timeout=1.5))
        observed = []
        result = client.send(json.dumps({"mode": "timeout"}), cwd=self.cwd, on_session=observed.append)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.succeeded)
        self.assertEqual(observed, [result.session_id])
        child_pid = int((self.cwd / "child.pid").read_text())
        if os.name == "nt":
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10,
            )
            self.assertNotIn(f'"{child_pid}"'.encode(), listing.stdout)
        else:
            # 部分 CI 的 init 延迟回收僵尸；僵尸已停止执行，不再写日志。
            stat = Path(f"/proc/{child_pid}/stat")
            if stat.exists():
                self.assertEqual(stat.read_text().split()[2], "Z")


if __name__ == "__main__":
    unittest.main()
