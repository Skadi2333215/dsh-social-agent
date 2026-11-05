"""账号拥有稳定的工作目录和会话编号，整个运行期间持有账号文件锁。"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from filelock import FileLock, Timeout


class AccountBusyError(RuntimeError):
    """同一账号已有 Python 运行占用了会话。"""


@dataclass(frozen=True)
class AccountSession:
    """一个实验账号对应的持久化身份；同一账号的后续运行沿用这组信息。"""

    # 研究者指定的稳定账号编号，如 account-001；不是 DSH 生成的会话编号。
    account_id: str

    # 该账号专用的绝对工作目录；DSH 续接时会核对目录，因此跨轮运行需要保持一致。
    cwd: Path

    # DSH 生成的 session-… 编号；None 表示账号还没有保存过会话，下一次需首次创建。
    session_id: str | None = None


class SessionStore:
    """管理账号目录、会话映射和文件锁；不负责调用模型或存放导出的分析日志。

    load()/save() 应在 lock() 上下文内使用，保证同一账号的读取、续接和写入串行完成。
    """

    def __init__(self, data_dir: Path):
        # 所有账号的父目录；其下按账号哈希分目录，分别保存 session.json、workspace 和锁文件。
        self.accounts_dir = data_dir.resolve() / "accounts"

    def account_dir(self, account_id: str) -> Path:
        """把账号编号转换为稳定、安全的目录路径；只计算路径，不创建文件夹。"""
        if not account_id.strip():
            raise ValueError("账号编号不能为空。")
        # 账号名可能含中文、斜杠或平台昵称；哈希避免路径穿越和 Windows 保留名。
        key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        return self.accounts_dir / key

    @contextmanager
    def lock(self, account_id: str) -> Iterator[None]:
        """在 with 语句范围内独占账号；被占用时立即抛出 AccountBusyError，不排队重试。

        文件锁可协调本项目启动的多个 Python 进程，退出 with 时自动释放。
        """
        directory = self.account_dir(account_id)
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(directory / "account.lock", timeout=0)
        try:
            lock.acquire()
        except Timeout as error:
            raise AccountBusyError(f"账号 {account_id!r} 正在运行，请等待本次运行结束。") from error
        try:
            yield
        finally:
            lock.release()

    def load(self, account_id: str) -> AccountSession:
        """读取账号映射并确保固定工作目录存在，由 runner 在持锁后调用。

        新账号尚无 session.json 时返回 session_id=None；已有文件损坏或目录改变时
        明确报错，避免用新会话悄悄覆盖原来的实验身份。
        """
        directory = self.account_dir(account_id)
        workspace = (directory / "workspace").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        state_path = directory / "session.json"
        if not state_path.exists():
            return AccountSession(account_id, workspace)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("account_id") != account_id:
            raise ValueError(f"账号映射不正确：{state_path}")
        if state.get("cwd") != str(workspace):
            raise ValueError(f"账号工作目录发生变化，请保留原目录后重试：{state_path}")
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"账号会话编号损坏：{state_path}")
        return AccountSession(account_id, workspace, session_id)

    def save(self, session: AccountSession) -> None:
        """把已经获得编号的账号身份原子写入 session.json，由 runner 在持锁后调用。

        已有会话只允许保存相同编号；拒绝空编号、目录变化和会话替换。
        """
        if not session.session_id:
            raise ValueError("不能保存空会话编号。")
        current = self.load(session.account_id)
        if current.cwd != session.cwd:
            raise ValueError("保存会话时工作目录不匹配。")
        if current.session_id not in (None, session.session_id):
            raise ValueError("DSH 返回了不同的会话编号，原账号映射已保留。")
        state = asdict(session)
        state["cwd"] = str(session.cwd)
        atomic_write_json(self.account_dir(session.account_id) / "session.json", state)


def atomic_write_json(path: Path, document: dict) -> None:
    """把 document 写入 path：先完整写入同目录临时文件，再原子替换目标文件。

    会话映射和 run.json 共用此函数，减少中断时留下半份 JSON 的可能。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
