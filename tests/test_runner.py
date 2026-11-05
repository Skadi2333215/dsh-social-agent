import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from social_agent.runner import AgentRunner
from social_agent.sessions import AccountBusyError
from tests.helpers import FakeHarnessClient, TEST_KEY, make_config


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = make_config(self.root)
        self.runner = AgentRunner(self.config, FakeHarnessClient(self.config))
        environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": TEST_KEY})
        environment.start()
        self.addCleanup(environment.stop)

    def test_complete_run_exports_inputs_outputs_and_provenance(self):
        result = self.runner.run("账号甲", "请记住研究代号：星河。")
        self.assertEqual(result.status, "completed", result.error)
        manifest = json.loads((result.output_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["session_id"], result.session_id)
        self.assertTrue(manifest["harness"]["succeeded"])
        self.assertEqual(len(manifest["export"]["sha256"]), 64)
        for name in ["prompt.txt", "reply.txt", "stdout.jsonl", "stderr.txt", "session.v4.jsonl.zstd", "session.jsonl"]:
            self.assertTrue((result.output_dir / name).is_file(), name)
        events = [json.loads(line) for line in (result.output_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["id"], result.session_id)
        self.assertEqual(events[1]["data"]["content"], "请记住研究代号：星河。")

    def test_changed_dotenv_is_loaded_again_and_redacted_on_each_run(self):
        config = replace(self.config, env_file=self.root / ".env")
        runner = AgentRunner(config, FakeHarnessClient(config))
        with patch.dict(os.environ):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            for key in ["local-first-secret", "local-second-secret"]:
                config.env_file.write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
                result = runner.run("one", key)
                self.assertEqual(result.status, "completed", result.error)
                self.assertEqual((result.output_dir / "prompt.txt").read_text(encoding="utf-8"), "[REDACTED]")

    def test_separate_python_process_resumes_same_session_and_full_log_history(self):
        first = self.runner.run("one", json.dumps({"message": "星河-7821"}))
        code = (
            "import json; from pathlib import Path; from tests.helpers import make_config,FakeHarnessClient; "
            "from social_agent.runner import AgentRunner; "
            f"config=make_config(Path({str(self.root)!r})); "
            "result=AgentRunner(config,FakeHarnessClient(config)).run('one',json.dumps({'mode':'recall'})); "
            "print(json.dumps({'session_id':result.session_id,'status':result.status,'reply':result.reply,'output_dir':str(result.output_dir)}))"
        )
        process = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        second = json.loads(process.stdout)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["session_id"], first.session_id)
        self.assertEqual(second["reply"], "星河-7821")
        self.assertNotEqual(Path(second["output_dir"]), first.output_dir)
        history = (Path(second["output_dir"]) / "session.jsonl").read_text(encoding="utf-8")
        self.assertEqual(sum(json.loads(line)["type"] == "user/message" for line in history.splitlines()), 2)

    def test_failure_still_saves_mapping_and_exports_partial_conversation(self):
        result = self.runner.run("one", json.dumps({"mode": "fail"}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.runner.sessions.load("one").session_id, result.session_id)
        self.assertTrue((result.output_dir / "session.jsonl").is_file())
        self.assertIn("模拟诊断", (result.output_dir / "stderr.txt").read_text(encoding="utf-8"))

    def test_timeout_still_exports_already_committed_log(self):
        config = make_config(self.root, timeout=1.5)
        result = AgentRunner(config, FakeHarnessClient(config)).run("one", json.dumps({"mode": "timeout"}))
        self.assertEqual(result.status, "timed_out")
        self.assertTrue((result.output_dir / "session.jsonl").is_file())
        self.assertEqual(self.runner.sessions.load("one").session_id, result.session_id)

    def test_export_failure_keeps_model_success_separate_and_retains_raw(self):
        result = self.runner.run("one", json.dumps({"mode": "corrupt-log"}))
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.reply)
        manifest = json.loads((result.output_dir / "run.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["harness"]["succeeded"])
        self.assertIn("截断", manifest["export"]["error"])
        self.assertTrue(Path(manifest["export"]["raw_path"]).is_file())
        self.assertFalse((result.output_dir / "session.jsonl").exists())

    def test_deleted_session_fails_without_resetting_account_mapping(self):
        first = self.runner.run("one", "hello")
        source = next((self.config.dsh_home / "sessions").glob(f"*/{first.session_id}/session.v4.jsonl.zstd"))
        source.unlink()
        result = self.runner.run("one", "continue")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.session_id, first.session_id)
        self.assertEqual(self.runner.sessions.load("one").session_id, first.session_id)

    def test_missing_key_produces_failure_manifest_without_creating_session(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            result = self.runner.run("one", "hello")
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.session_id)
        self.assertFalse((self.runner.sessions.account_dir("one") / "session.json").exists())
        self.assertTrue((result.output_dir / "run.json").is_file())

    def test_corrupted_mapping_does_not_start_harness(self):
        directory = self.runner.sessions.account_dir("one")
        directory.mkdir(parents=True)
        (directory / "session.json").write_text("invalid", encoding="utf-8")
        with patch.object(self.runner.harness, "send") as send:
            result = self.runner.run("one", "hello")
        send.assert_not_called()
        self.assertEqual(result.status, "failed")

    def test_same_account_concurrency_is_rejected(self):
        with self.runner.sessions.lock("one"):
            with self.assertRaises(AccountBusyError):
                self.runner.run("one", "hello")
