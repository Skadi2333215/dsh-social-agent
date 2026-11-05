"""显式开启后进行两次真实模型调用，保留运行产物供研究者检查。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from social_agent.config import load_api_key, load_config


@unittest.skipUnless(os.environ.get("SOCIAL_AGENT_LIVE_TEST") == "1", "设置 SOCIAL_AGENT_LIVE_TEST=1 后运行真实 DSH 测试")
class LiveHarnessTests(unittest.TestCase):
    def test_two_independent_processes_remember_a_random_token(self):
        self.assertTrue(load_api_key(load_config().env_file), "真实测试需要填写 .env 或设置 DEEPSEEK_API_KEY")
        token = "memory-" + uuid4().hex
        account_id = "live-check-" + uuid4().hex
        root = Path(__file__).resolve().parents[1]

        def run(prompt):
            process = subprocess.run(
                [sys.executable, "-m", "social_agent", "run", "--account", account_id, "--prompt", prompt, "--json"],
                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=340,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            return json.loads(process.stdout)

        first = run(f"请记住实验代号 {token}，只回复已记住。不要调用任何工具。")
        second = run("上一条消息要求你记住的实验代号是什么？只回复完整代号，不要调用任何工具。")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn(token, second["reply"])
        for result in [first, second]:
            directory = Path(result["output_dir"])
            rows = [json.loads(line) for line in (directory / "session.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["id"], result["session_id"])
            self.assertIn(token, (directory / "session.jsonl").read_text(encoding="utf-8"))
        print(f"\n真实测试归档：{second['output_dir']}")
