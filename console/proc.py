"""子进程封装 —— 本机两条铁律：

① 绝不用 subprocess.run(capture_output=True) 调 .bat/.ps1：
   孙进程继承 stdout 管道，父进程超时被杀后 communicate() 永远等不到 EOF（手册坑 1，代价 40 分钟死锁）。
② 任何常驻进程/服务必须无窗口拉起：CREATE_NO_WINDOW + 输出重定向到文件，绝不接管道。

本模块只提供两种入口，按"有没有孙进程"选：
- run_capture()：只给简单命令（python -c / schtasks / tasklist / powershell 单发）
- spawn_detached()：给服务与守护，立即返回 pid，永不等待
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def _hidden_kwargs() -> dict:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}


def run_capture(cmd: Sequence[str], timeout: float = 30, cwd: str | os.PathLike | None = None,
                encoding: str = "utf-8") -> dict[str, Any]:
    """跑一个"没有孙进程"的命令并收回输出。超时会强杀并返回 timed_out=True。"""
    started = __import__("time").time()
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding=encoding,
        errors="replace",
        **_hidden_kwargs(),
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", "killed after timeout"
    return {
        "cmd": list(cmd),
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "timed_out": timed_out,
        "elapsed_ms": int((__import__("time").time() - started) * 1000),
    }


def spawn_detached(cmd: Sequence[str], cwd: str | os.PathLike | None = None,
                   log_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """无窗口拉起常驻进程；返回 pid，不等待。stdout/stderr 直接写文件。"""
    log_file = None
    out_handle = err_handle = subprocess.DEVNULL
    if log_path:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        out_handle = open(log_file, "ab", buffering=0)
        err_handle = out_handle
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            cwd=str(cwd) if cwd else None,
            close_fds=True,
            **_hidden_kwargs(),
        )
    finally:
        if log_file is not None:
            try:
                out_handle.close()  # 子进程已继承句柄，父进程可关
            except Exception:
                pass
    return {"pid": proc.pid, "cmd": list(cmd), "log": str(log_file) if log_file else None}


def powershell_json(script: str, timeout: float = 25) -> Any:
    """跑一段 PowerShell 并解析 JSON（统一 UTF-8，避免中文乱码）。"""
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    res = run_capture(["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + script],
                      timeout=timeout, encoding="utf-8")
    out = (res.get("stdout") or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_parse_error": True, "raw": out[:2000], "stderr": (res.get("stderr") or "")[:500]}
