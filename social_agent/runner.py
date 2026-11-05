"""一次账号运行的主流程。阅读本文件即可理解调用和归档顺序。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .config import AppConfig, load_api_key
from .harness import HarnessClient, HarnessPort, HarnessResult, redact_secret
from .logs import ExportResult, LogExporter
from .sessions import SessionStore, atomic_write_json


@dataclass(frozen=True)
class RunResult:
    """交给命令行或 Python 调用方的一轮运行摘要，详细证据保存在 output_dir 中。

    一个账号可对应多轮运行：account_id 和 session_id 通常保持不变，run_id 每轮新建。
    """

    # 本轮使用的实验账号编号，由调用方指定，用来选择会话和固定工作目录。
    account_id: str

    # 本轮新生成的 UUID，用于唯一标记这次尝试；同一会话的不同轮次具有不同 run_id。
    run_id: str

    # 本轮创建或续接的 DSH 会话编号；首次调用未获得编号时为 None，续接失败时可保留原编号。
    session_id: str | None

    # 总体状态：模型和导出都成功为 completed；调用超时为 timed_out；其他失败为 failed。
    status: Literal["completed", "failed", "timed_out"]

    # DSH 最终回复，已遮盖已知密钥；没有取得回复时为空，导出失败时仍可保留模型回复。
    reply: str

    # 本轮独立归档目录的绝对路径，包含输入、回复、进程输出、会话日志和 run.json。
    output_dir: Path

    # 汇总后的失败原因，可能同时含调用失败和导出失败；成功时为 None。
    error: str | None = None


class AgentRunner:
    """组织一轮同步对话：读取身份、调用 DSH、保存映射、导出日志并返回摘要。

    harness 可传入满足 HarnessPort 的测试替身；省略时使用本地 DSH 客户端。
    当前主流程只处理 DSH 对话与归档，平台边界由 platform.py 单独定义。
    """

    def __init__(self, config: AppConfig, harness: HarnessPort | None = None):
        # 各模块共用的配置，决定模型、超时、账号数据和输出目录。
        self.config = config

        # 实际发送提示词的外部边界；正式运行使用 HarnessClient，测试时可替换。
        self.harness = HarnessClient(config) if harness is None else harness

        # 账号身份管理器，负责文件锁、固定工作目录和持久化的会话编号。
        self.sessions = SessionStore(config.data_dir)

        # 原始日志导出器，从 DSH 数据目录定位日志，再复制、解压并校验。
        self.logs = LogExporter(config.dsh_home)

        # 本轮已知密钥，初始为空、每轮重新读取；仅用于遮盖输出，不作为字段写入归档。
        self._api_key = ""

    def run(self, account_id: str, prompt: str) -> RunResult:
        """以 account_id 指定的账号发送一条 prompt，同步返回本轮摘要。

        相同账号会续接已有会话，并完整持锁至归档结束。账号已被占用时抛出
        AccountBusyError；提示词为空时抛出 ValueError。常见调用和导出失败写入结果。
        """
        if not prompt.strip():
            raise ValueError("提示词不能为空。")
        with self.sessions.lock(account_id):
            return self._run_locked(account_id, prompt)

    def _run_locked(self, account_id: str, prompt: str) -> RunResult:
        """已持有账号锁后的业务顺序；此方法只由 run() 调用。"""
        run_id = str(uuid4())
        started_at = utc_now()
        output_dir = self._create_output_dir(account_id, run_id)
        # 前置步骤也可能失败，先保留空身份与默认失败结果，使后续仍能写出运行说明。
        session_id = None
        workspace = None
        harness_result = HarnessResult(error="尚未启动 DSH。")
        export_result = ExportResult(error="尚未获得可导出的会话编号。")

        # 1. 读取账号会话；映射文件损坏时不启动新的对话。
        try:
            # 每轮读取，允许研究者在两次运行之间更新 .env。
            self._api_key = load_api_key(self.config.env_file)
            account = self.sessions.load(account_id)
            session_id, workspace = account.session_id, account.cwd

            def remember_session(identifier: str) -> None:
                """收到 session 事件后立即保存编号，避免模型超时后丢失已创建的会话。"""
                nonlocal session_id
                if account.session_id not in (None, identifier):
                    raise ValueError("返回的会话编号不同，原账号映射已保留。")
                session_id = identifier
                self.sessions.save(replace(account, session_id=identifier))

            # 2. 调用 DSH；读到 session 事件时立即写入映射。
            harness_result = self.harness.send(
                prompt, cwd=workspace, session_id=session_id, on_session=remember_session,
            )
            if harness_result.session_id is not None and harness_result.session_id != session_id:
                remember_session(harness_result.session_id)
        except Exception as error:
            # 外部调用和映射写盘失败后仍进入统一归档，保留已获得的证据。
            harness_result = replace(harness_result, error=self._redact(str(error)))
        except KeyboardInterrupt:
            harness_result = replace(harness_result, error="运行被手动中断。")

        # 3. 保存本次输入与进程输出。stdout 是控制流，分析数据来自下一步的原件。
        self._write_text(output_dir / "prompt.txt", prompt)
        self._write_text(output_dir / "reply.txt", harness_result.reply)
        self._write_text(output_dir / "stdout.jsonl", harness_result.stdout)
        self._write_text(output_dir / "stderr.txt", harness_result.stderr)

        # 4. 无论模型成功、失败或超时，只要编号已知就尝试导出。
        if session_id is not None and workspace is not None:
            export_result = self.logs.export(session_id, cwd=workspace, output_dir=output_dir)

        # 5. 分别记录模型运行和日志导出的状态；只有两者都成功才算完成。
        errors = [message for message in (harness_result.error, export_result.error) if message]
        if not harness_result.succeeded and harness_result.error is None:
            errors.append("DSH 未成功完成本次调用。")
        status = "timed_out" if harness_result.timed_out else "failed" if errors else "completed"
        error_message = self._redact("\n".join(errors)) if errors else None
        result = RunResult(
            account_id, run_id, session_id, status,
            self._redact(harness_result.reply), output_dir, error_message,
        )
        self._write_manifest(result, workspace, started_at, harness_result, export_result)
        return result

    def _create_output_dir(self, account_id: str, run_id: str) -> Path:
        """按账号、UTC 时间和本轮 UUID 创建新目录，避免覆盖此前的运行证据。"""
        account_key = self.sessions.account_dir(account_id).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        directory = self.config.output_dir / account_key / f"{timestamp}-{run_id}"
        directory.mkdir(parents=True, exist_ok=False)
        return directory.resolve()

    def _redact(self, text: str) -> str:
        """遮盖当前已知密钥，供本项目自行生成的文本和错误说明使用。"""
        return redact_secret(text, self._api_key)

    def _write_text(self, path: Path, text: str) -> None:
        """把经过密钥遮盖的文本按 UTF-8 写入文件；原始会话副本由 logs 原样保存。"""
        path.write_text(self._redact(text), encoding="utf-8", newline="")

    def _write_manifest(
        self, result: RunResult, workspace: Path | None, started_at: str,
        harness: HarnessResult, exported: ExportResult,
    ) -> None:
        """写入本轮 run.json，让分析程序可以追溯身份、时间、状态及原件校验值。

        result 是整体摘要；workspace 是原工作目录，未读到账号时为 None；
        started_at 是开始时间；harness 和 exported 分别提供调用和导出的详细结果。
        """
        export_document = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(exported).items()
        }
        if export_document["error"]:
            export_document["error"] = self._redact(export_document["error"])
        document = {
            # 本项目运行说明的结构版本，与 DSH 会话文件中的 format_version 分开记录。
            "schema_version": 1,
            "run_id": result.run_id,
            "account_id": result.account_id,
            "session_id": result.session_id,
            "cwd": str(workspace) if workspace is not None else None,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": result.status,
            "error": result.error,
            "model": self.config.model,
            "dsh_executable": str(self.config.dsh_executable) if self.config.dsh_executable else None,
            "launcher_mode": "cli" if self.config.dsh_command else "desktop",
            "dsh_home": str(self.config.dsh_home),
            # 单独保存调用状态：模型已回复但导出失败时，仍能判断模型调用本身成功。
            "harness": {
                "succeeded": harness.succeeded,
                "returncode": harness.returncode,
                "timed_out": harness.timed_out,
                "error": self._redact(harness.error) if harness.error else None,
            },
            # 导出结果含源路径、压缩副本路径、完整 JSONL 路径、事件数和压缩副本 SHA-256。
            "export": export_document,
        }
        atomic_write_json(result.output_dir / "run.json", document)


def utc_now() -> str:
    """归档时间统一记录时区，避免跨电脑分析时出现时间歧义。"""
    return datetime.now(timezone.utc).isoformat()
