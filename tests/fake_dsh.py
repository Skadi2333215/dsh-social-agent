"""测试用外部进程；其输出和日志明确属于虚拟 DSH。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import zstandard


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id")
    arguments = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    try:
        request = json.loads(prompt)
    except ValueError:
        request = {"mode": "success", "message": prompt}
    mode = request.get("mode", "success")
    session_id = arguments.session_id or "session-" + str(uuid4())
    directory = Path(os.environ["DSH_HOME"]) / "sessions" / "--fake-process--" / session_id
    log = directory / "session.v4.jsonl.zstd"
    if arguments.session_id and not log.exists():
        emit({"type": "error", "message": "session does not exist"})
        return 1
    directory.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(write_checksum=True)

    def append(event: dict) -> None:
        with log.open("ab") as handle:
            handle.write(compressor.compress((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")))

    if not log.exists():
        append({"type": "session", "id": session_id, "cwd": str(Path.cwd()), "version": 4, "fake": True})
    append({"type": "user/message", "data": {"content": prompt}})
    if mode != "missing-session":
        emit({
            "type": "session", "sessionId": "session-wrong" if mode == "wrong-session" else session_id,
            "cwd": str(Path.cwd().parent if mode == "wrong-cwd" else Path.cwd()),
        })
    if mode == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (Path.cwd() / "child.pid").write_text(str(child.pid))
        time.sleep(60)
    if mode == "large-stderr":
        sys.stderr.write("诊断" * 100_000)
        sys.stderr.flush()
    if mode == "invalid-json":
        print("not-json", flush=True)
    if mode == "fail":
        emit({"type": "error", "message": "模拟供应商失败"})
        print("模拟诊断", file=sys.stderr)
        return 1
    if mode == "secret":
        print(os.environ["DEEPSEEK_API_KEY"], file=sys.stderr)
    if mode == "no-final":
        return 0
    memory_file = directory / "fake-memory.json"
    previous = json.loads(memory_file.read_text(encoding="utf-8")) if memory_file.exists() else None
    memory_file.write_text(json.dumps(request.get("message"), ensure_ascii=False), encoding="utf-8")
    reply = str(previous) if mode == "recall" else "虚拟回复：" + request.get("message", "成功")
    if mode == "large-reply":
        reply = "中文长文本" * 10_000
    if mode == "secret":
        reply = os.environ["DEEPSEEK_API_KEY"]
    append({"type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": reply}]}}})
    append({"type": "turn/end", "data": {"reason": {"kind": "completed"}}})
    if mode == "corrupt-log":
        with log.open("ab") as handle:
            handle.write(b"\x28\xb5\x2f\xfd\x00")
    emit({"type": "status", "phase": "turn_end", "reason": {"kind": "error" if mode == "failed-turn" else "completed"}})
    emit({"type": "final", "text": reply})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
