"""测试夹具均写入临时目录，不使用桌面端历史会话或真实密钥。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import zstandard

from social_agent.config import AppConfig
from social_agent.harness import HarnessClient


TEST_KEY = "test-only-key-never-a-real-credential"


def make_config(root: Path, timeout: float = 10) -> AppConfig:
    return AppConfig(Path(sys.executable), root / "dsh", root / "data", root / "outputs", timeout_seconds=timeout)


class FakeHarnessClient(HarnessClient):
    """只替换启动命令，其余进程 I/O、超时和事件解析使用生产实现。"""

    def _build_command(self, patch_path: Path, session_id: str | None) -> list[str]:
        command = [sys.executable, str(Path(__file__).parent / "fake_dsh.py")]
        if session_id is not None:
            command.extend(["--session-id", session_id])
        return command


def write_log(
    home: Path, cwd: Path, session_id: str = "session-test-001", *,
    version: int = 4, events: list[dict] | None = None,
) -> tuple[Path, bytes]:
    directory = home / "sessions" / "--fixture--" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    name = "session.jsonl.zstd" if version == 0 else f"session.v{version}.jsonl.zstd"
    path = directory / name
    rows = [{"type": "session", "version": version, "id": session_id, "cwd": str(cwd.resolve())}]
    rows.extend(events or [])
    pieces = [(json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8") for row in rows]
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    path.write_bytes(b"".join(compressor.compress(piece) for piece in pieces))
    return path, b"".join(pieces)
