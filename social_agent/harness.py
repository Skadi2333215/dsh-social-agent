"""DSH 的外部进程边界：提示词走 stdin，stdout 只解析运行控制事件。"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, TextIO

from .config import AppConfig, load_api_key


@dataclass(frozen=True)
class HarnessResult:
    """一次 DSH 调用的结果；同时保存回复和排查失败需要的进程信息。

    此结果只描述 DSH 调用本身，日志是否成功导出由 ExportResult 单独记录。
    """

    # 已知的 DSH 会话编号；续接失败时仍可能保留传入编号，首次未获得编号时为 None。
    session_id: str | None = None

    # final 事件中的最终回复文本，已遮盖已知密钥；未收到最终回复时通常为空字符串。
    reply: str = ""

    # 子进程标准输出的完整收集文本，已遮盖已知密钥；内容是控制事件，不是原始分析日志。
    stdout: str = ""

    # 子进程标准错误输出，已遮盖已知密钥；用于诊断，非空本身不代表调用失败。
    stderr: str = ""

    # 子进程退出码，0 表示正常退出；没有获得进程退出结果时为 None。
    returncode: int | None = None

    # 是否因超过配置的秒数而终止；用于与普通进程报错区分。
    timed_out: bool = False

    # 本次调用的失败原因；None 表示没有记录到错误，不能仅凭此字段判断全部流程成功。
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """DSH 调用是否成功：没有错误、没有超时，并且子进程退出码为 0。"""
        return self.error is None and not self.timed_out and self.returncode == 0


class HarnessPort(Protocol):
    """DSH 调用边界；正式客户端和测试替身提供同样的 send() 方法即可。"""

    def send(
        self, prompt: str, *, cwd: Path, session_id: str | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> HarnessResult:
        """发送 prompt 并同步等待本轮结果。

        cwd 是账号的固定工作目录；session_id 为 None 时新建会话，否则续接该会话。
        on_session 是可选回调，收到会话编号后立即调用，用于在模型结束前保存账号映射。
        """
        ...


@dataclass
class _RunOutput:
    """解析 stdout 时使用的内部暂存对象，由 stdout 线程更新、主线程收集结果。

    它负责校验 DSH 返回的身份、提取回复和收集错误，不负责解压原始会话日志。
    """

    # 预期的账号绝对工作目录，用于核对 session 事件中的 cwd。
    cwd: Path

    # 调用前已保存的会话编号；新账号为 None，续接时用于防止 DSH 意外切换到其他会话。
    expected_session_id: str | None

    # 首次读到合法 session 事件时执行的回调；None 表示调用方不需要立即保存编号。
    on_session: Callable[[str], None] | None

    # 本次实际从 session 事件读到的编号；尚未收到合法事件时为 None。
    session_id: str | None = None

    # 已解析出的最终回复；只取 final 事件，不拼接中途显示的文本片段。
    reply: str = ""

    # 是否收到了格式正确的 final 事件；用于识别进程退出却没有最终结果的情况。
    final_received: bool = False

    # 按到达顺序保存的 stdout 原始文本行，包含换行；每个实例各自持有一份列表。
    lines: list[str] = field(default_factory=list)

    # 协议解析、DSH 报错和回调失败的说明；收集后继续读管道，最终统一判定失败。
    errors: list[str] = field(default_factory=list)

    def read(self, stream: TextIO) -> None:
        """持续读取 stdout；单个事件解析失败后仍排空后续输出，避免进程被管道卡住。"""
        with stream:
            for line in stream:
                self.lines.append(line)
                if not line.strip():
                    continue
                try:
                    self.accept(json.loads(line))
                except Exception as error:
                    # 必须继续排空管道，否则大量输出会卡住子进程。
                    # 这里也收集回调写盘失败；主线程会把本次运行判为失败。
                    self.errors.append(f"读取 DSH 事件失败：{error}")

    def accept(self, event: object) -> None:
        """处理已知事件中的会话、结束状态和最终回复；其余事件原文由 read() 留存。"""
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("stdout 中存在非事件 JSON。")
        if event["type"] == "session":
            identifier = event.get("sessionId")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("session 事件缺少会话编号。")
            if self.expected_session_id is not None and identifier != self.expected_session_id:
                raise ValueError("DSH 返回的会话编号与待续接会话不一致。")
            if self.session_id is not None and identifier != self.session_id:
                raise ValueError("同一次运行返回了多个会话编号。")
            if not isinstance(event.get("cwd"), str) or Path(event["cwd"]).resolve() != self.cwd:
                raise ValueError("DSH 返回的工作目录与账号工作目录不一致。")
            first_session_event = self.session_id is None
            self.session_id = identifier
            if first_session_event and self.on_session is not None:
                self.on_session(identifier)
        elif event["type"] == "final":
            if not isinstance(event.get("text"), str):
                raise ValueError("final 事件缺少回复文本。")
            self.reply = event["text"]
            self.final_received = True
        elif event["type"] == "error":
            self.errors.append("DSH 报错：" + str(event.get("message", "未知错误")))
        elif event["type"] == "status" and event.get("phase") == "turn_end":
            reason = event.get("reason")
            if not isinstance(reason, dict) or reason.get("kind") != "completed":
                self.errors.append("DSH 回合未正常完成。")


class HarnessClient:
    """通过本地桌面版的 headless 入口完成一轮对话，不自动重试失败调用。"""

    def __init__(self, config: AppConfig):
        # 共用的运行配置，提供 DSH 安装路径、模型、密钥文件位置和调用超时时间。
        self.config = config

    def send(
        self, prompt: str, *, cwd: Path, session_id: str | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> HarnessResult:
        """把 prompt 通过 UTF-8 标准输入交给 DSH，并同步返回 HarnessResult。

        cwd 必须是已存在的账号工作目录；session_id 为 None 表示首次对话。
        续接时沿用原编号和原目录。on_session 在收到编号时通知 runner 立即保存映射。
        常见配置、启动和模型失败写入返回值的 error，超时还会设置 timed_out。
        """
        if not prompt.strip():
            return HarnessResult(session_id=session_id, error="提示词不能为空。")
        try:
            key = load_api_key(self.config.env_file)
        except (OSError, ValueError) as error:
            return HarnessResult(session_id=session_id, error=f"读取 API Key 配置失败：{error}")
        if not key.strip():
            return HarnessResult(session_id=session_id, error="缺少 DEEPSEEK_API_KEY，请填写项目 .env 文件或设置环境变量。")
        if not cwd.is_dir():
            return HarnessResult(session_id=session_id, error=f"工作目录不存在：{cwd}")

        with tempfile.TemporaryDirectory(prefix="social-agent-dsh-") as temporary:
            patch_path = Path(temporary) / "model.patch.yml"
            patch_path.write_text(self.model_patch(), encoding="utf-8")
            try:
                command = self._build_command(patch_path, session_id)
                return self._execute(command, prompt, cwd.resolve(), session_id, on_session, key)
            except OSError as error:
                return HarnessResult(session_id=session_id, error=redact_secret(f"启动 DSH 失败：{error}", key))
            except ValueError as error:
                return HarnessResult(session_id=session_id, error=redact_secret(str(error), key))

    def model_patch(self) -> str:
        """生成本轮临时配置：指定模型、环境变量密钥来源，并关闭桌面登录凭据来源。

        JSON 是 YAML 的子集，因此可用标准库编码；配置中只写密钥变量名，不写密钥值。
        """
        return json.dumps([
            {"id": "agent-default-model", "config": {
                "provider": "deepseek-official", "model": self.config.model,
            }},
            {"id": "llm-deepseek", "config": {"apiKeyEnv": "DEEPSEEK_API_KEY"}},
            {"id": "credentials", "disabled": True},
            {"id": "authorization", "disabled": True},
            {"id": "deepseek-account", "disabled": True},
        ], ensure_ascii=False, indent=2) + "\n"

    def launch_prefix(self) -> list[str]:
        """检查本机启动入口并返回参数前缀，供实际调用和 doctor 共用。

        独立 CLI 可以跨环境配置；Windows 的批处理入口改用 node 与脚本路径，
        避免把提示词交给 cmd.exe 再解释。桌面版直接使用自身的 Electron 运行时。
        """
        if self.config.dsh_command:
            command = list(self.config.dsh_command)
            program = shutil.which(command[0])
            if program is None:
                raise ValueError("找不到 dsh_command 指定的程序，请检查安装位置或 PATH。")
            if os.name == "nt" and Path(program).suffix.lower() in {".cmd", ".bat"}:
                raise ValueError("Windows 的 dsh_command 请使用 node.exe 与 CLI 脚本路径，或直接配置 DSH 桌面版入口。")
            command[0] = program
            return command
        executable = self.config.dsh_executable
        if executable is None or not executable.is_file():
            raise ValueError("找不到 DSH 入口，请配置 dsh_executable、AGENT_DSH_EXECUTABLE 或 dsh_command。")
        if self.config.resources_dir is None or not (self.config.resources_dir / "app.asar").is_file():
            raise ValueError("DSH 安装不完整：资源目录缺少 app.asar。")
        # 与桌面版自带 dsh.cmd 一致，直接启动 exe，避开 cmd.exe 的二次转义。
        return [
            str(executable), "--expose-internals", str(self.config.cli_script),
        ]

    def _build_command(self, patch_path: Path, session_id: str | None) -> list[str]:
        """组装通用 headless 参数；末尾的 '-' 表示让 DSH 从标准输入读取提示词。"""
        command = self.launch_prefix() + [
            "--profile", "headless", "--patch", str(patch_path), "--json",
        ]
        if session_id is not None:
            command.extend(["--session-id", session_id])
        return command + ["-"]

    def _execute(
        self, command: list[str], prompt: str, cwd: Path, session_id: str | None,
        on_session: Callable[[str], None] | None, key: str,
    ) -> HarnessResult:
        """启动子进程、同时处理三条 I/O 通道，并把运行证据汇总为结果。"""
        environment = os.environ.copy()
        # Electron 同时支持桌面和 Node 模式；此处使用 Node 模式运行随安装包提供的脚本。
        if self.config.dsh_command:
            environment.pop("ELECTRON_RUN_AS_NODE", None)
        else:
            environment["ELECTRON_RUN_AS_NODE"] = "1"
        environment["DSH_HOME"] = str(self.config.dsh_home)
        environment["DEEPSEEK_API_KEY"] = key
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=environment, text=True, encoding="utf-8", errors="replace",
            shell=False, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        output = _RunOutput(cwd, session_id, on_session)
        stderr_parts: list[str] = []
        input_errors: list[str] = []

        def read_stderr() -> None:
            """独立排空诊断输出，避免 stderr 塞满时阻塞 DSH。"""
            with process.stderr:
                stderr_parts.append(process.stderr.read())

        def write_prompt() -> None:
            """发送提示词并关闭 stdin，使读取 '-' 的 DSH 能确定输入已结束。"""
            try:
                with process.stdin:
                    process.stdin.write(prompt)
            except (OSError, ValueError) as error:
                input_errors.append(f"发送提示词失败：{error}")

        # 三条 I/O 通道同时排空；对外仍然是同步的一次 send 调用。
        threads = [
            threading.Thread(target=output.read, args=(process.stdout,), daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
            threading.Thread(target=write_prompt, daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._stop_process_tree(process)
        except BaseException:
            self._stop_process_tree(process)
            raise
        finally:
            for thread in threads:
                thread.join(timeout=5)

        errors = output.errors + input_errors
        if timed_out:
            errors.insert(0, f"DSH 调用超过 {self.config.timeout_seconds:g} 秒，已终止进程树。")
        elif process.returncode != 0:
            errors.insert(0, f"DSH 进程失败，退出码 {process.returncode}；请查看 stderr.txt。")
        if any(thread.is_alive() for thread in threads):
            errors.append("DSH 的输入输出通道未正常关闭。")
        if output.session_id is None:
            errors.append("未收到本次运行的 session 事件。")
        if not output.final_received:
            errors.append("未收到最终回复事件。")
        return HarnessResult(
            session_id=output.session_id or session_id,
            reply=redact_secret(output.reply, key),
            stdout=redact_secret("".join(output.lines), key),
            stderr=redact_secret("".join(stderr_parts), key),
            returncode=process.returncode, timed_out=timed_out,
            error=redact_secret("\n".join(dict.fromkeys(errors)), key) if errors else None,
        )

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen) -> None:
        """超时后终止整棵进程树，避免后台写入导致日志快照不稳定。"""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def redact_secret(text: str, secret: str | None = None) -> str:
    """只遮盖本次已知 API Key；原始 DSH 会话归档由 logs 原样保存。"""
    secret = os.environ.get("DEEPSEEK_API_KEY", "") if secret is None else secret
    return text.replace(secret, "[REDACTED]") if secret else text
