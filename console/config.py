"""路径与配置加载。所有配置都是 UTF-8（容忍 BOM）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ops-console/console/config.py -> ops-console/ -> ai-gateways/
CONSOLE_DIR = Path(__file__).resolve().parent.parent
ROOT = CONSOLE_DIR.parent
CONFIG_DIR = CONSOLE_DIR / "config"
STATE_DIR = CONSOLE_DIR / "state"
LOG_DIR = CONSOLE_DIR / "logs"

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    """读配置/状态；容忍 BOM；解析失败抛明确异常（不吞）。"""
    for attempt in range(10):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except PermissionError:
            # Windows：另一进程正在 os.replace 的瞬间会命中，必须重试
            import time

            time.sleep(0.15)
        except FileNotFoundError:
            return None
    raise RuntimeError(f"读取失败（持续被占用）：{path}")


def load_services() -> dict:
    return load_json(CONFIG_DIR / "services.json") or {}


def load_policies() -> dict:
    return load_json(CONFIG_DIR / "policies.json") or {}


def python_exe(prefer_windowless: bool = True) -> str:
    """返回可用的解释器路径。

    pythonw.exe 下 sys.executable 是 pythonw，不能直接拿它跑需要日志的子进程；
    这里统一返回同目录的 python.exe（没有则回落 sys.executable）。
    """
    import sys

    exe = Path(sys.executable)
    plain = exe.with_name("python.exe")
    if exe.name.lower() == "pythonw.exe" and plain.exists():
        return str(plain)
    return str(exe if not prefer_windowless else (plain if plain.exists() else exe))


def pythonw_exe() -> str:
    """返回 pythonw.exe（无控制台），用于拉起常驻服务。"""
    import sys

    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    if w.exists():
        return str(w)
    return str(exe)


def load_secrets() -> dict:
    """本机密钥（config/secrets.local.json，已被 .gitignore 排除）。"""
    return load_json(CONFIG_DIR / "secrets.local.json") or {}


def secret(name: str) -> str:
    """取一个密钥；缺失时返回空串，由调用方显式报错，绝不静默用错值。"""
    return str(load_secrets().get(name) or "")


def expand(value: str) -> str:
    """展开命令参数里的 {secret:name} 占位符。"""
    if isinstance(value, str) and value.startswith("{secret:") and value.endswith("}"):
        return secret(value[len("{secret:"):-1])
    return value


def resolve(rel: str) -> Path:
    """配置文件里的相对路径一律相对 ai-gateways 根目录。"""
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p).resolve()
