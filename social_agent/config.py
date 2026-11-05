"""集中读取配置；这里不启动模型，也不读取桌面端的凭据文件。"""

from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """一次运行需要的配置，由 load_config() 读取后交给各模块共用。

    路径在读取配置时转换为绝对路径；frozen=True 表示创建后不能直接改属性，
    避免调用过程中配置发生变化，导致会话与导出目录不一致。
    """

    # 本机 DSH 桌面程序的可执行文件路径；Windows 为 exe，macOS 为 .app 内的程序。
    # None 表示未找到桌面版，可改用 dsh_command 指定独立 CLI。
    dsh_executable: Path | None

    # DSH 自己的数据根目录，通常为用户目录下的 .dsh；原始日志位于其 sessions 子目录。
    dsh_home: Path

    # 本项目的账号数据根目录：保存账号与会话的映射、固定工作目录和账号文件锁。
    data_dir: Path

    # 本项目的分析产物根目录；每轮在这里新建目录，保存回复、日志副本和运行说明。
    output_dir: Path

    # 交给 DSH 使用的模型名称；通过临时配置指定，默认使用 deepseek-flash。
    model: str = "deepseek-flash"

    # 等待一次 DSH 子进程完成的最长时间，单位为秒；超过后终止进程树并尝试归档。
    timeout_seconds: float = 300

    # 备用密钥文件路径；环境变量优先。None 表示不读取文件，文件缺失也视为未提供密钥。
    env_file: Path | None = None

    # 可选的独立 CLI 启动参数，如 ("dsh",) 或 ("node", "/opt/dsh/cli.js")。
    # 非空时优先于桌面版；仅放启动器，headless 等参数由 harness 统一补上。
    dsh_command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """dataclass 创建实例后自动调用，拦截空模型名和无效超时时间。"""
        if not self.model.strip():
            raise ValueError("model 不能为空。")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限的正数。")
        if not isinstance(self.dsh_command, tuple) or any(
            not isinstance(part, str) or not part.strip() for part in self.dsh_command
        ):
            raise ValueError("dsh_command 必须是由非空字符串组成的参数列表。")

    @property
    def resources_dir(self) -> Path | None:
        """桌面版安装资源目录；兼容 Windows/Linux 和 macOS 应用包的目录布局。"""
        if self.dsh_executable is None:
            return None
        executable_dir = self.dsh_executable.parent
        if executable_dir.name == "MacOS" and executable_dir.parent.name == "Contents":
            return executable_dir.parent / "Resources"
        return executable_dir / "resources"

    @property
    def cli_script(self) -> Path | None:
        """由 exe 位置推导出的 headless 入口脚本；这是 app.asar 内的虚拟路径。

        该脚本交给 DSH 自带的 Electron/Node 运行，不是本项目提供的 Python 文件。
        未找到 exe 时返回 None；此属性只计算路径，不检查安装是否完整。
        """
        if self.resources_dir is None:
            return None
        return (
            self.resources_dir / "app.asar" / "dsh"
            / "node_modules" / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js"
        )


def load_config(path: Path | None = None) -> AppConfig:
    """读取 TOML 和环境变量，返回经过基本校验的运行配置。

    path 是可选的 TOML 文件路径。优先级为环境变量 > TOML > 默认值；
    相对路径及 .env 的位置以配置文件目录为准，未传 path 时以当前运行目录为准。
    此处只确定 .env 的位置，密钥内容在调用 DSH 时单独读取。
    """
    base = Path.cwd().resolve() if path is None else path.resolve().parent
    values: dict = {}
    if path is not None:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        if set(document) - {"agent"}:
            raise ValueError("配置文件只接受 [agent] 节。")
        values = document.get("agent", {})
        if not isinstance(values, dict):
            raise ValueError("[agent] 必须是一张配置表。")
        supported = {"dsh_executable", "dsh_command", "dsh_home", "data_dir", "output_dir", "model", "timeout_seconds"}
        unknown = set(values) - supported
        if unknown:
            raise ValueError("未知配置项：" + ", ".join(sorted(unknown)))

    def setting(name: str, default: str) -> str:
        """按优先级读取单个配置项，并统一转换为字符串供后续解析。"""
        environment_name = "DSH_HOME" if name == "dsh_home" else "AGENT_" + name.upper()
        value = os.environ.get(environment_name, values.get(name, default))
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name} 的配置类型不正确。")
        return str(value)

    def directory(name: str, default: str) -> Path:
        """把配置中的路径转换为绝对路径；不在这里创建目录。"""
        value = setting(name, default)
        if not value.strip():
            raise ValueError(f"{name} 不能为空。")
        result = Path(value).expanduser()
        return (base / result).resolve()

    executable_text = setting("dsh_executable", "")
    executable = directory("dsh_executable", "") if executable_text else find_dsh_executable()
    command = values.get("dsh_command", [])
    if not isinstance(command, list) or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ValueError("dsh_command 必须是由非空字符串组成的 TOML 数组。")
    # 显式桌面路径优先于自动发现；Unix 上的 dsh 启动脚本可直接作为进程入口。
    if not command and not executable_text and executable is None and os.name != "nt":
        if installed_command := shutil.which("dsh"):
            command = [installed_command]
    if command and ("/" in command[0] or "\\" in command[0]):
        command[0] = str((base / Path(command[0]).expanduser()).resolve())
    try:
        timeout = float(setting("timeout_seconds", "300"))
    except ValueError as error:
        raise ValueError("timeout_seconds 必须是秒数。") from error

    return AppConfig(
        dsh_executable=executable,
        dsh_home=directory("dsh_home", str(Path.home() / ".dsh")),
        data_dir=directory("data_dir", ".agent-data"),
        output_dir=directory("output_dir", "outputs"),
        model=setting("model", "deepseek-flash"),
        timeout_seconds=timeout,
        env_file=base / ".env",
        dsh_command=tuple(command),
    )


def load_api_key(env_file: Path | None) -> str:
    """返回 API Key；环境变量优先，其次读取 env_file 指定的 .env。

    未提供密钥时返回空字符串；文件格式有误时抛出 ValueError。
    环境变量即使为空也优先于文件，避免悄悄使用另一个来源的密钥。
    只读取 DEEPSEEK_API_KEY，不执行变量展开，也不修改当前进程的环境变量。
    """
    if "DEEPSEEK_API_KEY" in os.environ:
        return os.environ["DEEPSEEK_API_KEY"]
    if env_file is None or not env_file.exists():
        return ""
    found = False
    key = ""
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.strip().partition("=")
        if name.strip() != "DEEPSEEK_API_KEY":
            continue
        if found or not separator:
            raise ValueError(".env 中 DEEPSEEK_API_KEY 必须只出现一次，并使用 名称=值 格式。")
        found = True
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(".env 中 API Key 的引号没有正确闭合。")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        key = value
    return key


def find_dsh_executable() -> Path | None:
    """寻找桌面版入口；Windows 使用安装目录和快捷方式，macOS 检查应用包。

    Linux 的安装布局不同，优先用 PATH 中的独立 dsh，或在配置中指定桌面版可执行文件。
    """
    candidates: list[Path] = []
    if command := shutil.which("dsh"):
        shim = Path(command).resolve()
        if shim.name.lower() == "dsh.cmd" and len(shim.parents) > 4:
            candidates.append(shim.parents[4] / "DeepSeek Harness.exe")
    if local_data := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local_data) / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe")
    if sys.platform == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            candidates.append(applications / "DeepSeek Harness.app" / "Contents" / "MacOS" / "DeepSeek Harness")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    if os.name != "nt":
        return None
    # 命令内容固定，不拼接账号、提示词或用户输入。
    script = (
        "$shortcut = Join-Path ([Environment]::GetFolderPath('ApplicationData')) "
        "'Microsoft\\Windows\\Start Menu\\Programs\\DeepSeek Harness.lnk'; "
        "if (Test-Path -LiteralPath $shortcut) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "(New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
        candidate = Path(result.stdout.strip())
        return candidate.resolve() if candidate.is_file() else None
    except (OSError, subprocess.TimeoutExpired):
        return None
