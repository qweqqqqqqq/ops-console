"""单实例锁 —— 用锁文件 + pid 判活，绝不使用 os.kill(pid, 0)。

Windows 没有"信号 0"：除 CTRL_C_EVENT/CTRL_BREAK_EVENT 外任何 signal 都会走
TerminateProcess，等于把被探查的进程杀掉（且 exit code=0，看起来像正常退出）。
判活一律走 tasklist。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import atomic, proc


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    res = proc.run_capture(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=15, encoding="gbk")
    out = (res.get("stdout") or "").strip()
    return str(pid) in out and "No tasks" not in out and "信息" not in out


class Lock:
    def __init__(self, path: str | os.PathLike, name: str = "console"):
        self.path = Path(path)
        self.name = name
        self.held = False

    def acquire(self, wait_sec: float = 0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.0, wait_sec)
        while True:
            payload = {"pid": os.getpid(), "name": self.name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    import json

                    fh.write(json.dumps(payload, ensure_ascii=False))
                self.held = True
                return True
            except FileExistsError:
                holder = atomic.read_json(self.path, default={}) or {}
                pid = int(holder.get("pid") or 0)
                if not pid_alive(pid):
                    # 陈旧锁（进程已死）：清掉重试
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        if not self.held:
            return
        try:
            holder = atomic.read_json(self.path, default={}) or {}
            if int(holder.get("pid") or 0) == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.held = False

    def __enter__(self):
        if not self.acquire(wait_sec=10):
            raise RuntimeError(f"另一个控制台实例正在运行（锁：{self.path}）")
        return self

    def __exit__(self, *exc):
        self.release()
        return False
