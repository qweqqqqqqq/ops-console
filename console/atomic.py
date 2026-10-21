"""状态文件读写 —— Windows 上"原子写 + 并发读"必须两侧都重试。

写侧：tmp 文件 + fsync + os.replace（替换是原子的）
读侧：os.replace 瞬间 open() 会抛 PermissionError（实测 300 次里撞 13~14 次）
两侧都重试，才能保证高频读写不崩。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

RETRIES = 10
DELAY = 0.15


def write_json(path: str | os.PathLike, obj: Any, indent: int = 2) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    data = json.dumps(obj, ensure_ascii=False, indent=indent)
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            for _ in range(RETRIES):  # replace 也可能被读方占用
                try:
                    os.replace(tmp, p)
                    return p
                except PermissionError as exc:
                    last_err = exc
                    time.sleep(DELAY)
            raise RuntimeError(f"os.replace 持续失败：{p}（{last_err}）")
        except PermissionError as exc:
            last_err = exc
            time.sleep(DELAY)
    raise RuntimeError(f"写入失败：{p}（{last_err}）")


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # 容忍"写到一半"：重试一次再判失败
                time.sleep(DELAY)
                continue
        except FileNotFoundError:
            return default
        except PermissionError as exc:
            last_err = exc
            time.sleep(DELAY)
    if last_err:
        raise RuntimeError(f"读取失败：{p}（{last_err}）")
    return default


def append_jsonl(path: str | os.PathLike, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    for attempt in range(RETRIES):
        try:
            with open(p, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
            return
        except PermissionError:
            time.sleep(DELAY)
    raise RuntimeError(f"追加失败：{p}")
