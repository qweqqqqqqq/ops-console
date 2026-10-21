"""自动体检 —— 把 config/rules.json 里 check=auto 的规矩落成代码。

只读：不改任何状态。输出 [{id, severity, ok, detail}]。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import config, state

# 保留端口 -> 该端口上应出现的命令行特征（不匹配即视为归属异常）
PORT_OWNER_HINTS: dict[int, tuple[str, ...]] = {
    8790: ("qoder_proxy",),
    8798: ("agent_memory_os", "agent-memory-lab"),
    8420: ("tencentdb-agent-memory", "tsx"),
    8310: ("embeddings", "server.py"),
    9999: ("ultron",),
}


def _fmt_age(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} 分钟"
    if minutes < 60 * 48:
        return f"{minutes/60:.1f} 小时"
    return f"{minutes/1440:.1f} 天"


def check_forbidden(cards: list[dict]) -> dict:
    # 已废弃/退役的服务期望就是不在线（例如 3001 FreeLLMAPI），不算异常
    bad = [c for c in cards if c["forbidden"] and not c["listening"] and not c.get("expect_down")]
    retired = [c for c in cards if c.get("expect_down")]
    detail = "禁区服务均已监听" if not bad else \
             "禁区服务未监听（不自动拉起，需人工）：" + "、".join(f"{c['name']}(:{c['port']})" for c in bad)
    if retired:
        detail += "；已退役：" + "、".join(f"{c['name']}" for c in retired)
    return {"id": "R1", "severity": "error", "ok": not bad, "detail": detail}


def check_duplicates(procs: list[dict]) -> dict:
    import re

    counts: dict[str, list[int]] = {}
    for p in procs:
        cmd = p.get("cmd") or ""
        m = re.search(r"([\w\-\u4e00-\u9fff]+\.(?:py|bat|cmd|vbs))", cmd)
        if not m:
            continue
        counts.setdefault(m.group(1), []).append(p.get("pid"))
    dup = {k: v for k, v in counts.items() if len(v) > 1}
    detail = "无重复实例" if not dup else "重复实例：" + "；".join(
        f"{k}×{len(v)}（pid {','.join(str(x) for x in v)}）" for k, v in list(dup.items())[:6])
    return {"id": "R2", "severity": "warn", "ok": not dup, "detail": detail}


def check_state_files() -> dict:
    spec = (config.load_json(config.CONFIG_DIR / "lines.json") or {}).get("state_files") or []
    stale, missing = [], []
    for item in spec:
        rel = item.get("path")
        max_age = item.get("max_age_min")
        if not rel or item.get("retired"):
            continue
        path = config.resolve(rel)
        if not path.exists():
            missing.append(rel)
            continue
        if max_age is None:
            continue
        age_min = (time.time() - path.stat().st_mtime) / 60
        if age_min > float(max_age):
            stale.append(f"{rel} 已 {_fmt_age(age_min)}未更新（阈值 {max_age} 分钟）")
    ok = not stale
    detail = "；".join(stale) if stale else "关键状态文件均在时效内"
    if missing:
        detail += f"；缺文件：{', '.join(missing[:4])}"
    return {"id": "R3", "severity": "warn", "ok": ok, "detail": detail}


def check_big_logs() -> dict:
    spec = (config.load_json(config.CONFIG_DIR / "lines.json") or {}).get("state_files") or []
    big = []
    for item in spec:
        limit = item.get("max_size_mb")
        rel = item.get("path")
        if not limit or not rel:
            continue
        path = config.resolve(rel)
        if not path.exists():
            continue
        mb = path.stat().st_size / (1024 * 1024)
        if mb > float(limit):
            big.append(f"{rel} {mb:.1f}MB > {limit}MB")
    return {"id": "R4", "severity": "warn", "ok": not big,
            "detail": "；".join(big) if big else "日志体积正常"}


def check_disk() -> dict:
    warn_gb = float((config.load_policies().get("health") or {}).get("disk_free_gb_warn", 20))
    usage = shutil.disk_usage(str(config.ROOT))
    free_gb = usage.free / (1024 ** 3)
    return {"id": "R5", "severity": "info", "ok": free_gb >= warn_gb,
            "detail": f"{config.ROOT.drive} 剩余 {free_gb:.0f}GB（警戒 {warn_gb:.0f}GB）"}


def check_port_owner(ports: dict[int, int], procs: list[dict]) -> dict:
    by_pid = {p.get("pid"): (p.get("cmd") or "") for p in procs}
    problems = []
    for port, hints in PORT_OWNER_HINTS.items():
        pid = ports.get(port)
        if pid is None:
            continue
        cmd = by_pid.get(pid, "")
        if cmd and not any(h in cmd for h in hints):
            problems.append(f":{port} 被 pid {pid} 占用，但命令行不像 {'/'.join(hints)}：{cmd[:80]}")
        elif not cmd:
            problems.append(f":{port} 被 pid {pid} 占用，命令行不可见（可能非本栈进程）")
    return {"id": "R6", "severity": "error", "ok": not problems,
            "detail": "；".join(problems) if problems else "保留端口归属正常"}


def check_newapi_db() -> dict:
    """R7：New API 库一致性（只读）。规则来源：流水线全貌 §12.3/§14。"""
    from . import newapi_db

    res = newapi_db.audit()
    if not res.get("ok"):
        return {"id": "R7", "severity": "error", "ok": False,
                "detail": f"库审计不可用：{res.get('error')}", "findings": []}
    errs = [f for f in res["findings"] if f["level"] == "error"]
    warns = [f for f in res["findings"] if f["level"] == "warn"]
    if errs or warns:
        shown = (errs + warns)[:2]
        detail = "；".join(f["detail"] for f in shown)
        if len(errs + warns) > len(shown):
            detail += f"（另有 {len(errs + warns) - len(shown)} 类，见 manage.py db）"
    else:
        detail = "库一致性正常（group 无逗号 / 无孤儿 abilities / 无残留路由 / Key 正常）"
    return {"id": "R7", "severity": "error", "ok": not errs, "detail": detail,
            "findings": res["findings"]}


def run_all(snap: dict | None = None) -> list[dict]:
    snap = snap or state.snapshot()
    ports = snap.get("_ports") or {}
    if not ports:
        ports = {c["port"]: c["pid"] for c in snap.get("services", []) if c.get("pid")}
    procs = snap.get("_procs") or snap.get("daemons", [])
    results = [
        check_forbidden(snap.get("services", [])),
        check_duplicates(procs),
        check_state_files(),
        check_big_logs(),
        check_disk(),
        check_port_owner(ports, procs),
        check_newapi_db(),
    ]
    return results
