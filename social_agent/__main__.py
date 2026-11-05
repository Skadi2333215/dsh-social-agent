"""命令行只负责输入输出，具体业务交给 runner。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import AppConfig, load_api_key, load_config
from .harness import HarnessClient, redact_secret
from .logs import LogExporter
from .runner import AgentRunner


def build_parser() -> argparse.ArgumentParser:
    """声明命令行用法：doctor 检查环境，run 运行一轮，export 单独导出已有会话。"""
    parser = argparse.ArgumentParser(description="调用本地 DSH 并导出完整会话日志。")
    parser.add_argument("--config", type=Path, help="TOML 配置文件；相对路径以该文件目录为准")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查安装位置和密钥是否就绪，不调用模型")

    run = commands.add_parser("run", help="以一个账号发起或续接一轮对话")
    run.add_argument("--account", required=True, help="研究者指定的稳定账号编号")
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="本次发送的消息")
    prompt.add_argument("--prompt-file", type=Path, help="UTF-8 提示词文件")
    run.add_argument("--json", action="store_true", help="以 JSON 返回运行结果和导出位置")

    export = commands.add_parser("export", help="单独导出已有会话，不调用模型")
    export.add_argument("--session-id", required=True)
    export.add_argument("--cwd", type=Path, required=True, help="会话创建时的工作目录")
    export.add_argument("--output-dir", type=Path, required=True, help="新的导出目录")
    return parser


def doctor(config: AppConfig) -> int:
    """显示路径与密钥是否就绪；具备调用条件返回 0，否则返回 1，不发送模型请求。"""
    installation_error = None
    try:
        HarnessClient(config).launch_prefix()
    except (OSError, ValueError) as error:
        installation_error = str(error)
    key_ready = bool(load_api_key(config.env_file).strip())
    print(f"启动方式：{'独立 CLI' if config.dsh_command else '桌面版 headless'}")
    print(f"DSH 入口：{config.dsh_command[0] if config.dsh_command else config.dsh_executable or '未找到'}")
    print(f"安装资源：{installation_error or '就绪'}")
    print(f"DSH 数据目录：{config.dsh_home}")
    print(f"账号数据目录：{config.data_dir}")
    print(f"日志输出目录：{config.output_dir}")
    print(f"模型：{config.model}")
    print(f"超时：{config.timeout_seconds:g} 秒")
    print(f"密钥文件：{config.env_file or '未指定'}")
    print(f"DEEPSEEK_API_KEY：{'已设置' if key_ready else '未设置'}")
    return 0 if installation_error is None and key_ready else 1


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回进程退出码；argv 为 None 时读取终端参数，传列表便于测试。"""
    # Windows 管道默认可能使用系统代码页；命令行 JSON 始终输出 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    arguments = build_parser().parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.command == "doctor":
            return doctor(config)
        if arguments.command == "export":
            # 导出不需要调用模型，因此不检查密钥；其结果结构为 ExportResult。
            result = LogExporter(config.dsh_home).export(
                arguments.session_id, cwd=arguments.cwd.resolve(),
                output_dir=arguments.output_dir.resolve(),
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
            return 0 if result.succeeded else 1

        prompt = arguments.prompt
        if arguments.prompt_file is not None:
            prompt = arguments.prompt_file.read_text(encoding="utf-8-sig")
        result = AgentRunner(config).run(arguments.account, prompt)
        if arguments.json:
            # 将 dataclass 转成 JSON；Path 通过 default=str 转成供其他程序读取的路径文本。
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        else:
            print(f"状态：{result.status}")
            print(f"会话：{result.session_id or '尚未创建'}")
            print(f"导出目录：{result.output_dir}")
            if result.reply:
                print("\n" + result.reply)
            if result.error:
                print("\n" + result.error, file=sys.stderr)
        return 0 if result.status == "completed" else 1
    except (OSError, ValueError, RuntimeError) as error:
        print("运行失败：" + redact_secret(str(error)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
