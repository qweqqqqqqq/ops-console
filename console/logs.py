"""决策日志 —— 每个动作都留证据链，供事后复盘与 Ultron 经验抽取。

两种输出：
- logs/console.jsonl：机器可读（who/what/why/before/after/evidence/took_ms）
- 终端：人类可读
"""
from __future__ import annotations

import sys
import time
from typing import Any

from . import atomic, config


def _jsonl_path():
    pol = config.load_policies().get("safety", {})
    name = pol.get("decision_log", "decisions.jsonl")
    return config.LOG_DIR / name


def record(action: str, **fields: Any) -> dict:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "pid": __import__("os").getpid(),
        **fields,
    }
    try:
        atomic.append_jsonl(_jsonl_path(), entry)
    except Exception as exc:  # 日志失败不能影响主流程，但要让人看见
        print(f"[warn] 决策日志写入失败：{exc}", file=sys.stderr)
    return entry


def safe_print(text: str = "") -> None:
    """pythonw 下 sys.stdout 是 None —— 先兜底再写。"""
    if sys.stdout is None:
        sys.stdout = open(__import__("os").devnull, "w", encoding="utf-8")
    print(text)
