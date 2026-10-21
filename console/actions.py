"""执行层 —— 白名单动作：start / stop / restart。

铁律（每一条都由代码强制，不依赖调用方自觉）：
1. 未在 services.json 登记 → 拒绝
2. forbidden / 未 allow_start / 未 allow_stop → 拒绝
3. 端口在 policies.forbidden.restart 里 → 拒绝（配置被改坏也拦得住）
4. 破坏性动作要确认令牌（环境变量 OPS_CONSOLE_CONFIRM == 动作 id）
5. 写操作必须持单实例锁
6. 启动后必须在 verify_window 内监听，否则杀掉回滚
7. 只按 PID 停，绝不 taskkill /IM
"""
from __future__ import annotations

import os
import socket
import time
from typing import Any

from . import atomic, config, lock, logs, proc, state

ACTIONS_USED: list[str] = []


class Refused(RuntimeError):
    """请求被安全策略拒绝（不是程序错误）。"""


def _svc(key: str) -> dict:
    for s in (config.load_services().get("services") or []):
        if s.get("key") == key:
            return s
    raise Refused(f"未登记的服务 key：{key}（白名单见 config/services.json）")


def _forbidden_ports() -> set[int]:
    pol = config.load_policies()
    return {int(p) for p in ((pol.get("forbidden") or {}).get("restart") or [])}


def _check_allowed(svc: dict, action_id: str) -> None:
    port = int(svc.get("port") or 0)
    if svc.get("forbidden"):
        raise Refused(f"{svc.get('name')} 是禁区服务（{svc.get('reason') or '禁止启停'}）")
    if port in _forbidden_ports():
        raise Refused(f":{port} 在 policies.forbidden.restart 名单里，拒绝 {action_id}")
    if action_id == "service.start" and not svc.get("allow_start"):
        raise Refused(f"{svc.get('name')} 未授予 allow_start（{svc.get('reason') or ''}）")
    if action_id in ("service.stop", "service.restart") and not svc.get("allow_stop"):
        raise Refused(f"{svc.get('name')} 未授予 allow_stop（{svc.get('reason') or ''}）")


def _needs_confirm(action_id: str) -> bool:
    pol = config.load_policies().get("safety") or {}
    return action_id in (pol.get("require_confirm_token") or [])


def _confirm_ok(action_id: str) -> bool:
    if not _needs_confirm(action_id):
        return True
    pol = config.load_policies().get("safety") or {}
    env_name = pol.get("confirm_env") or "OPS_CONSOLE_CONFIRM"
    return os.environ.get(env_name, "").strip() == action_id


def _budget_ok() -> None:
    pol = config.load_policies().get("safety") or {}
    cap = int(pol.get("max_actions_per_run") or 3)
    if len(ACTIONS_USED) >= cap:
        raise Refused(f"本次运行已达动作上限 {cap} 个（防止一次批量动生产）")


def _console_lock():
    pol = config.load_policies().get("safety") or {}
    return lock.Lock(config.LOG_DIR / os.path.basename(pol.get("lock_file") or "console.lock"))


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


def _pid_on_port(port: int) -> int | None:
    return state.listening_ports().get(int(port))


def _verify(port: int, window: int) -> dict:
    started = time.time()
    while time.time() - started < window:
        if _port_open(port):
            http = state.http_probe(port, timeout=3)
            return {"ok": True, "waited_ms": int((time.time() - started) * 1000), "http": http}
        time.sleep(1)
    return {"ok": False, "waited_ms": int((time.time() - started) * 1000), "http": None}


def _kill_pid(pid: int) -> dict:
    """只按 PID 精确停止（禁 taskkill /IM）。"""
    res = proc.run_capture(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=15, encoding="gbk")
    return {"pid": pid, "rc": res.get("returncode"), "out": (res.get("stdout") or "").strip()[:120]}


def start(key: str, apply: bool = False) -> dict:
    svc = _svc(key)
    _check_allowed(svc, "service.start")
    port = int(svc.get("port") or 0)
    spec = svc.get("start") or {}
    cmd_tpl = spec.get("cmd") or []
    if not cmd_tpl:
        raise Refused(f"{key} 没有配置启动命令（config/services.json → services[].start.cmd）")

    before = {"listening": _port_open(port), "pid": _pid_on_port(port)}
    if before["listening"]:
        return {"ok": True, "skipped": True, "reason": "已在监听", "before": before}

    cmd = []
    for part in cmd_tpl:
        p = config.expand(str(part))
        if p == "{pythonw}":
            cmd.append(config.pythonw_exe())
        elif p == "{python}":
            cmd.append(config.python_exe())
        else:
            if not p:
                raise Refused(f"{key} 的启动命令含未配置的密钥占位符（检查 config/secrets.local.json）")
            cmd.append(p)

    plan = {"action": "service.start", "key": key, "port": port, "cmd": cmd, "before": before}
    if not apply:
        return {"ok": True, "dry_run": True, "plan": plan,
                "hint": "加 --apply 才会真的启动；破坏性动作还需确认令牌"}
    if not _confirm_ok("service.start"):
        raise Refused("需要确认令牌：set OPS_CONSOLE_CONFIRM=service.start")

    with _console_lock():
        _budget_ok()
        log_path = config.LOG_DIR / f"{key}.log"
        spawned = proc.spawn_detached(cmd, cwd=str(config.ROOT), log_path=log_path)
        window = int((config.load_policies().get("safety") or {}).get("verify_window_sec") or 60)
        verdict = _verify(port, window)
        ACTIONS_USED.append("service.start")
        result = {"ok": verdict["ok"], "pid": spawned["pid"], "log": spawned["log"], "verify": verdict}
        if not verdict["ok"]:
            kill = _kill_pid(spawned["pid"])
            result["rollback"] = kill
            result["hint"] = f"启动后 {window}s 未监听，已回滚；看 {log_path} 的尾部"
        logs.record("service.start", key=key, port=port, apply=apply, **result)
        return result


def stop(key: str, apply: bool = False) -> dict:
    svc = _svc(key)
    _check_allowed(svc, "service.stop")
    port = int(svc.get("port") or 0)
    pid = _pid_on_port(port)
    before = {"listening": pid is not None, "pid": pid}
    if pid is None:
        return {"ok": True, "skipped": True, "reason": "未在监听", "before": before}

    plan = {"action": "service.stop", "key": key, "port": port, "pid": pid, "before": before}
    if not apply:
        return {"ok": True, "dry_run": True, "plan": plan}
    if not _confirm_ok("service.stop"):
        raise Refused("需要确认令牌：set OPS_CONSOLE_CONFIRM=service.stop")

    with _console_lock():
        _budget_ok()
        kill = _kill_pid(pid)
        deadline = time.time() + 20
        while time.time() < deadline and _port_open(port):
            time.sleep(0.5)
        released = not _port_open(port)
        ACTIONS_USED.append("service.stop")
        result = {"ok": released, "kill": kill, "port_released": released}
        logs.record("service.stop", key=key, port=port, apply=apply, **result)
        return result


def restart(key: str, apply: bool = False) -> dict:
    svc = _svc(key)
    _check_allowed(svc, "service.restart")
    if not apply:
        return {"ok": True, "dry_run": True,
                "plan": {"action": "service.restart", "key": key, "port": svc.get("port")}}
    stopped = stop(key, apply=True)
    started = start(key, apply=True)
    return {"ok": bool(stopped.get("ok")) and bool(started.get("ok")), "stop": stopped, "start": started}
