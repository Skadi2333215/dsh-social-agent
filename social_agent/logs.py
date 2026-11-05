"""只读 DSH 会话源文件，保留压缩原件并导出无损 JSONL。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import zstandard


@dataclass(frozen=True)
class ExportResult:
    """一次日志导出的结果；成功时提供完整日志，失败时说明保留了哪些排查文件。"""

    # DSH 数据目录中的源压缩文件位置；还未定位到唯一源文件时为 None。
    source_path: Path | None = None

    # 导出目录中的压缩副本位置；失败时也可能保留文件，便于重新解压和检查。
    raw_path: Path | None = None

    # 完整解压并通过校验的 session.jsonl 路径；None 表示尚无可供分析的完整日志。
    jsonl_path: Path | None = None

    # 未完成导出时保留的 session.jsonl.partial 路径；可能只含部分数据，成功时为 None。
    partial_path: Path | None = None

    # 导出压缩副本的 SHA-256 校验值，用于复核该快照的字节内容；尚未算出时为 None。
    sha256: str | None = None

    # 完整 JSONL 校验通过后的事件条数，包含会话头和累计历史；导出失败时保留默认值 0。
    event_count: int = 0

    # 日志格式版本，即 session.v4.jsonl.zstd 中的 4；不是运行轮次，失败时可能为 None。
    format_version: int | None = None

    # 定位、复制、解压或归属校验的失败说明；None 表示没有记录到导出错误。
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """是否已得到完整日志：没有导出错误，并且 jsonl_path 已设置。"""
        return self.error is None and self.jsonl_path is not None


class LogExporter:
    """只读 DSH 的会话源目录，把原件和完整解压结果归档到指定输出目录。"""

    def __init__(self, dsh_home: Path):
        # DSH 的原始会话存储根目录；按工作目录分组，其下再按 session_id 存放会话。
        self.sessions_dir = dsh_home.resolve() / "sessions"

    def export(self, session_id: str, *, cwd: Path, output_dir: Path) -> ExportResult:
        """导出指定会话的累计历史，返回文件位置、校验值及导出状态。

        session_id 决定查找哪个会话；cwd 用于核对日志头中的原工作目录。
        output_dir 是本轮归档目录，可已存在，但不能已有同名导出文件。
        应在 DSH 本轮退出后调用，使源文件完成写入。常见失败写入结果的 error；
        保留压缩副本及 .partial 供排查，只有 jsonl_path 指向通过校验的完整日志。
        """
        source = raw = partial = None
        digest = None
        try:
            source, version = self._find_log(session_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            raw = output_dir / source.name
            partial = output_dir / "session.jsonl.partial"
            destination = output_dir / "session.jsonl"
            if any(path.exists() for path in (raw, partial, destination)):
                raise ValueError("导出目标已有文件，请使用新的运行目录。")
            shutil.copyfile(source, raw)
            digest = file_sha256(raw)
            # 先写 .partial；解压及归属校验全部通过后再改名，防止分析时误用半份日志。
            decompress_frames(raw, partial)
            event_count = validate_jsonl(partial, session_id, cwd.resolve(), version)
            partial.replace(destination)
            return ExportResult(
                source_path=source, raw_path=raw, jsonl_path=destination,
                sha256=digest, event_count=event_count, format_version=version,
            )
        except (OSError, ValueError, UnicodeError, zstandard.ZstdError) as error:
            return ExportResult(
                source_path=source, raw_path=raw if raw is not None and raw.exists() else None,
                partial_path=partial if partial is not None and partial.exists() else None,
                sha256=digest, error=f"日志导出失败：{error}",
            )

    def _find_log(self, session_id: str) -> tuple[Path, int]:
        """按编号定位唯一会话目录，返回最高格式版本的压缩文件路径和版本号。

        比较的是数字版本，例如 v10 高于 v9；旧格式 session.jsonl.zstd 视为版本 0。
        """
        if re.fullmatch(r"session-[A-Za-z0-9-]+", session_id) is None:
            raise ValueError("会话编号不是安全的 session-… 路径段。")
        directories = [path for path in self.sessions_dir.glob(f"*/{session_id}") if path.is_dir()]
        if len(directories) != 1:
            raise ValueError(f"应当找到唯一的会话目录，实际找到 {len(directories)} 个：{session_id}")
        generations: list[tuple[int, Path]] = []
        for path in directories[0].iterdir():
            match = re.fullmatch(r"session\.v([1-9][0-9]*)\.jsonl\.zstd", path.name)
            if match and path.is_file():
                generations.append((int(match.group(1)), path))
            elif path.name == "session.jsonl.zstd" and path.is_file():
                generations.append((0, path))
        if not generations:
            raise ValueError(f"会话目录内没有规范命名的压缩日志：{directories[0]}")
        version, path = max(generations, key=lambda item: item[0])
        return path, version


def decompress_frames(source: Path, destination: Path) -> None:
    """把 source 中连续拼接的 Zstandard 压缩帧完整解压到新文件 destination。

    一份 DSH 日志可能包含多个压缩帧，因此读完第一帧后还要处理后续帧。
    每帧单独检查 EOF，防止把截断文件误判为完整日志；失败时保留已写出的部分内容。
    """
    decoder = zstandard.ZstdDecompressor().decompressobj()
    frame_open = False
    frames = 0
    with source.open("rb") as compressed, destination.open("xb") as decoded:
        while chunk := compressed.read(64 * 1024):
            pending = chunk
            while pending:
                frame_open = True
                decoded.write(decoder.decompress(pending))
                if decoder.eof:
                    # 当前读取块可能同时包含下一帧；剩余字节交给新的解压器继续处理。
                    pending = decoder.unused_data
                    frames += 1
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                    frame_open = False
                else:
                    pending = b""
        if frame_open:
            raise ValueError("最后一个 Zstandard 帧被截断，已保留原件和部分解压文件。")
        if frames == 0:
            raise ValueError("压缩日志为空。")


def validate_jsonl(path: Path, session_id: str, cwd: Path, version: int) -> int:
    """核对会话头的编号、工作目录和格式版本，并返回全部有效事件的数量。

    逐行检查 JSON 与事件类型；未知事件及其字段原样保留，空白行不计数。
    校验只读取 path，不改写解压结果，任一事件损坏时立即报错。
    """
    count = 0
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise ValueError(f"JSONL 第 {line_number} 行损坏。") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError(f"JSONL 第 {line_number} 行不是有效事件。")
            if count == 0:
                if event.get("type") != "session" or event.get("id") != session_id:
                    raise ValueError("日志头的会话编号不匹配。")
                if not isinstance(event.get("cwd"), str) or Path(event["cwd"]).resolve() != cwd:
                    raise ValueError("日志头的工作目录不匹配。")
                if event.get("version") != version:
                    raise ValueError("日志头版本与文件名版本不一致。")
            count += 1
    if count == 0:
        raise ValueError("解压后的日志没有会话头。")
    return count


def file_sha256(path: Path) -> str:
    """分块计算文件字节的 SHA-256，供归档清单记录压缩快照的校验值。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
