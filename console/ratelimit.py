"""全局频率闸门 —— 跨进程生效。

为什么必须做成持久化的：本栈已知限流红线（邮件发码 38s、geekerone 190s、
登录 20 次/20 分钟），而调用方可能是计划任务、守护、还是我（AI）——
多进程各自 sleep 等于没限速。所以把 last_at 落盘，谁调用都先取号。
"""
from __future__ import annotations

import time
from typing import Any

from . import atomic, config, lock

STATE = config.STATE_DIR / "ratelimit.json"
LOCK = config.LOG_DIR / "ratelimit.lock"

DEFAULTS: dict[str, float] = {
    "email_verification": 38.0,   # 邮件发码（坑 21 / 作战手册）
    "geekerone_register": 190.0,  # geekerone 注册（坑 20）
    "login": 60.0,                # 登录接口 20 次/20 分钟 → 视作 60s 一次
    "site_probe": 5.0,            # 站点只读探测
    "key_validate": 3.0,          # 验 Key
}


def _load() -> dict[str, Any]:
    return atomic.read_json(STATE, default={}) or {}


def wait_time(domain: str, interval: float | None = None) -> dict:
    """还需等多少秒才能取号（不写入状态）。"""
    interval = DEFAULTS.get(domain, 60.0) if interval is None else interval
    data = _load()
    last = float((data.get(domain) or {}).get("last_at") or 0)
    elapsed = (time.time() - last) if last else 0.0
    remain = max(0.0, interval - elapsed)
    return {"domain": domain, "interval": interval, "last_at": last,
            "elapsed_sec": round(elapsed, 1) if last else None, "wait_sec": round(remain, 1), "ready": remain <= 0}


def acquire(domain: str, interval: float | None = None) -> dict:
    """取号：ok=True 才允许发起请求；否则返回还需等待的秒数。"""
    info = wait_time(domain, interval)
    if not info["ready"]:
        return {**info, "ok": False}
    with lock.Lock(LOCK, name="ratelimit") as _lk:
        info = wait_time(domain, interval)  # 拿到锁后复查，避免并发穿透
        if not info["ready"]:
            return {**info, "ok": False}
        data = _load()
        data[domain] = {"last_at": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        atomic.write_json(STATE, data)
    return {**info, "ok": True}


def snapshot() -> dict:
    data = _load()
    out = {}
    for domain, interval in DEFAULTS.items():
        out[domain] = wait_time(domain, interval)
    return {"state_file": str(STATE), "domains": out}

