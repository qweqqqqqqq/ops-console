"""状态聚合 —— 只读。把散在四处的运行态合成一个快照：

1. config/services.json   —— 我们承认的服务清单（唯一事实源）
2. svc_manager.py --json  —— 既有服务管理器的健康结论
3. 监听端口 / 进程命令行   —— 实况（PowerShell 单发查询）
4. schtasks 相关任务       —— 守护与定时作业

绝不在此模块里改任何系统状态（除写自己的日志）。
"""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from typing import Any

from . import config, proc

TASK_KEYWORDS = ("补号", "补货", "签到", "checkin", "restock", "newapi", "gateway",
                 "svc", "adapter", "opencode", "中轉", "中转", "farm", "pool")


def listening_ports(timeout: float = 25) -> dict[int, int]:
    """port -> pid（只取 LISTEN）。"""
    script = ("Get-NetTCPConnection -State Listen | ForEach-Object { "
              "[pscustomobject]@{p=[int]$_.LocalPort; pid=[int]$_.OwningProcess} } | ConvertTo-Json -Compress")
    data = proc.powershell_json(script, timeout=timeout)
    out: dict[int, int] = {}
    if isinstance(data, dict):
        data = [data]
    for row in data or []:
        try:
            out[int(row["p"])] = int(row["pid"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def process_index(timeout: float = 30) -> list[dict]:
    """本栈相关进程（命令行含 ai-gateways 或已知关键词）。"""
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -ne $null -and "
        "($_.CommandLine -like '*ai-gateways*' -or $_.CommandLine -like '*qoder_proxy*' -or "
        "$_.CommandLine -like '*cf_adapter*' -or $_.CommandLine -like '*chatglm_adapter*' -or "
        "$_.CommandLine -like '*media_adapter*' -or $_.CommandLine -like '*svc_manager*' -or "
        "$_.CommandLine -like '*pgw.py*' -or $_.CommandLine -like '*restock*' -or "
        "$_.CommandLine -like '*checkin*' -or $_.CommandLine -like '*pool-refresh*' -or "
        "$_.CommandLine -like '*ai4-*') } | "
        "Select-Object ProcessId,Name,@{n='cmd';e={$_.CommandLine}} | ConvertTo-Json -Compress -Depth 3"
    )
    data = proc.powershell_json(script, timeout=timeout)
    if isinstance(data, dict):
        data = [data]
    rows = []
    for row in data or []:
        cmd = (row.get("cmd") or "")
        rows.append({"pid": row.get("ProcessId"), "name": row.get("Name"), "cmd": cmd[:400]})
    return rows


def cmdlines(pids, timeout: float = 25) -> dict[int, str]:
    """按 pid 批量取命令行（用于校验保留端口归属，含非本栈进程）。"""
    wanted = sorted({int(p) for p in pids if p})
    if not wanted:
        return {}
    arr = ",".join(str(p) for p in wanted)
    script = (f"$ids=@({arr}); Get-CimInstance Win32_Process | Where-Object {{ $ids -contains $_.ProcessId }} | "
              "Select-Object ProcessId,Name,@{n='cmd';e={$_.CommandLine}} | ConvertTo-Json -Compress -Depth 3")
    data = proc.powershell_json(script, timeout=timeout)
    if isinstance(data, dict):
        data = [data]
    out: dict[int, str] = {}
    for row in data or []:
        try:
            out[int(row["ProcessId"])] = row.get("cmd") or ""
        except (KeyError, TypeError, ValueError):
            continue
    return out


def svc_manager_status(timeout: float = 40) -> dict:
    mgr = (config.load_services().get("svc_manager") or {})
    path = config.resolve(mgr.get("path", "..\\svc_manager.py"))
    if not path.exists():
        return {"error": f"svc_manager 不存在：{path}"}
    res = proc.run_capture([config.python_exe(), str(path), "--json"], timeout=timeout,
                           cwd=str(config.ROOT), encoding="utf-8")
    if res.get("timed_out"):
        return {"error": "svc_manager --json 超时"}
    raw = (res.get("stdout") or "").strip()
    if not raw:
        return {"error": f"svc_manager 无输出（rc={res.get('returncode')}）：{(res.get('stderr') or '')[:200]}"}
    import json

    try:
        return {"services": json.loads(raw)}
    except json.JSONDecodeError:
        return {"error": "svc_manager 输出不是 JSON", "raw": raw[:300]}


def scheduled_tasks(timeout: float = 40) -> list[dict]:
    res = proc.run_capture(["schtasks", "/Query", "/FO", "CSV", "/NH"], timeout=timeout, encoding="gbk")
    rows = []
    for line in (res.get("stdout") or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 3:
            continue
        name, status = parts[0].strip('"'), parts[1]
        if name == "TaskName":
            continue
        if any(k in name for k in TASK_KEYWORDS):
            rows.append({"name": name, "status": status})
    return rows


def http_probe(port: int, timeout: float = 3.0) -> dict:
    url = f"http://127.0.0.1:{port}/"
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return {"alive": True, "status": resp.status, "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"alive": True, "status": exc.code, "ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"alive": False, "status": None, "ms": int((time.time() - started) * 1000), "error": str(exc)[:120]}


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def snapshot(probe_http: bool = True) -> dict:
    cfg = config.load_services()
    services = cfg.get("services") or []
    reserved = cfg.get("reserved_ports") or []
    ports = listening_ports()
    procs = process_index()
    # 保留端口的持有者可能不匹配本栈关键词，单独补取命令行（否则归属校验永远拿不到证据）
    watched = {int(rp.get("port") or 0) for rp in reserved}
    watched |= {int(svc.get("port") or 0) for svc in services}
    extra = {ports[p] for p in watched if p in ports} - {p["pid"] for p in procs}
    if extra:
        for pid, cmd in cmdlines(extra).items():
            procs.append({"pid": pid, "name": "?", "cmd": cmd})
    by_pid = {p["pid"]: p for p in procs}
    mgr = svc_manager_status()

    cards: list[dict] = []
    findings: list[dict] = []

    for svc in services:
        port = int(svc.get("port") or 0)
        pid = ports.get(port)
        card = {
            "key": svc.get("key"),
            "name": svc.get("name"),
            "port": port,
            "role": svc.get("role"),
            "listening": pid is not None,
            "pid": pid,
            "managed_by_us": bool(svc.get("managed_by_us")),
            "forbidden": bool(svc.get("forbidden")),
            "expect_down": bool(svc.get("expect_down")),
            "retired": bool(svc.get("retired")),
            "allow_start": bool(svc.get("allow_start")),
            "allow_stop": bool(svc.get("allow_stop")),
            "holder": (by_pid.get(pid) or {}).get("cmd", "")[:160] if pid else "",
        }
        if probe_http and card["listening"]:
            card["http"] = http_probe(port)
        cards.append(card)

        if svc.get("forbidden") and not card["listening"]:
            findings.append({"level": "info", "code": "forbidden-down", "msg": f"{svc.get('name')}(:{port}) 未监听——禁区服务，不自动拉起"})
        elif card["managed_by_us"] and not card["listening"]:
            findings.append({"level": "warn", "code": "managed-down", "msg": f"{svc.get('name')}(:{port}) 未监听，可一键拉起"})
        elif not card["listening"] and not svc.get("forbidden"):
            findings.append({"level": "info", "code": "down", "msg": f"{svc.get('name')}(:{port}) 未监听（外部管理）"})

    # 端口归属校验：保留端口被别人占了 = 冲突（这正是 8790 那次的教训）
    port_owner: dict[int, str] = {}
    for rp in reserved:
        p = int(rp.get("port") or 0)
        port_owner[p] = rp.get("owner") or ""
        pid = ports.get(p)
        if pid is None:
            findings.append({"level": "info", "code": "reserved-free", "msg": f"保留端口 :{p}（{rp.get('owner')}）当前空闲"})
        else:
            cmd = (by_pid.get(pid) or {}).get("cmd", "")
            findings.append({"level": "info", "code": "reserved-holder",
                             "msg": f"保留端口 :{p} 由 pid {pid} 占用：{cmd[:100] or '(非本栈进程)'}"})

    # 配置内重名端口
    seen: dict[int, list[str]] = {}
    for svc in services:
        seen.setdefault(int(svc.get("port") or 0), []).append(svc.get("key") or "?")
    for port, keys in seen.items():
        if len(keys) > 1:
            findings.append({"level": "error", "code": "duplicate-port", "msg": f"配置里 :{port} 被多个服务登记：{keys}"})

    if mgr.get("error"):
        findings.append({"level": "warn", "code": "svc-manager", "msg": f"svc_manager 状态不可用：{mgr['error']}"})

    daemons = [p for p in procs if any(k in (p["cmd"] or "") for k in
                                       ("restock", "checkin", "pool-refresh", "ai4-", "farm"))]

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "counts": {
            "services": len(cards),
            "listening": sum(1 for c in cards if c["listening"]),
            "managed": sum(1 for c in cards if c["managed_by_us"]),
            "forbidden": sum(1 for c in cards if c["forbidden"]),
            "processes": len(procs),
            "daemons": len(daemons),
        },
        "services": cards,
        "svc_manager": mgr,
        "daemons": daemons,
        "tasks": scheduled_tasks(),
        "findings": findings,
        # 内部用（供 checks 复用，输出 JSON 时以 _ 前缀过滤）
        "_ports": ports,
        "_procs": procs,
    }
