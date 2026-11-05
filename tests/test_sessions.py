from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from social_agent.sessions import AccountBusyError, SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root)

    def test_session_and_workspace_survive_new_store_instance(self):
        with self.store.lock("账号甲"):
            initial = self.store.load("账号甲")
            self.store.save(replace(initial, session_id="session-one"))
        with SessionStore(self.root).lock("账号甲"):
            restored = SessionStore(self.root).load("账号甲")
        self.assertEqual(initial.cwd, restored.cwd)
        self.assertEqual(restored.session_id, "session-one")

    def test_account_names_cannot_escape_the_data_directory(self):
        for identifier in ["../../outside", "CON", "用户/甲", "用户\\乙"]:
            path = self.store.account_dir(identifier)
            self.assertEqual(path.parent, self.root.resolve() / "accounts")
        self.assertNotEqual(self.store.account_dir("a/b"), self.store.account_dir("a\\b"))

    def test_session_cannot_be_silently_replaced(self):
        with self.store.lock("one"):
            session = self.store.load("one")
            self.store.save(replace(session, session_id="session-first"))
            with self.assertRaisesRegex(ValueError, "原账号映射已保留"):
                self.store.save(replace(session, session_id="session-second"))
            self.assertEqual(self.store.load("one").session_id, "session-first")

    def test_changed_workspace_and_corrupted_mapping_fail_loudly(self):
        with self.store.lock("one"):
            session = self.store.load("one")
            self.store.save(replace(session, session_id="session-first"))
            path = self.store.account_dir("one") / "session.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["cwd"] = str(self.root / "wrong")
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "工作目录"):
                self.store.load("one")
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.store.load("one")

    def test_same_account_is_rejected_across_processes_but_other_account_runs(self):
        code = (
            "from pathlib import Path; from social_agent.sessions import SessionStore,AccountBusyError; "
            f"store=SessionStore(Path({str(self.root)!r})); "
            "\ntry:\n with store.lock('one'): pass\nexcept AccountBusyError: raise SystemExit(23)"
        )
        with self.store.lock("one"):
            with self.store.lock("two"):
                self.assertTrue(self.store.load("two").cwd.is_dir())
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 23, result.stderr)
            with self.assertRaises(AccountBusyError):
                with self.store.lock("one"):
                    pass
        with self.store.lock("one"):
            pass
