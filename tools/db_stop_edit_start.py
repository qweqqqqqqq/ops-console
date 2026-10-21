#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""停 New API → 改库 → 起回（唯一能真正生效的路径）。

为什么必须停：New API 进程持有渠道缓存并会周期性写回数据库，
实测外部直接改库会在 2 分钟内被覆盖（连 channels.models 都被写回）。

流程（对应《流水线全貌》§12.2）：
  1) 备份 one-api.db
  2) 停 :3000（只按 PID，try/finally 保证起回）
  3) 单事务写：channels.models='' + abilities.enabled=0 + 停空 Key 渠道 + ch25 恢复
  4) 起回 :3000（无窗口，日志落盘），等端口监听
  5) 验证：/v1/models + 一次真实 chat

用法：python tools\\db_stop_edit_start.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from console import logs  # noqa: E402

DB = r"D:\wb2api\new-api\one-api.db"
NEWAPI_EXE = r"D:\wb2api\new-api\bin\new-api.exe"
NEWAPI_LOG_DIR = r"D:\wb2api\new-api\logs"
API_KEY = "LR6WlQ2tKdpipbmfeOX3PmbuCCJA6U0mJwZKF2S1JffYLuBO"
CREATE_NO_WINDOW = 0x08000000


def port_open(port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_on_port(port: int) -> int | None:
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          f"(Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8", timeout=20,
                             creationflags=CREATE_NO_WINDOW)
        v = (out.stdout or "").strip()
        return int(v) if v.isdigit() else None
    except Exception:
        return None


def stop_newapi() -> dict:
    pid = pid_on_port(3000)
    if not pid:
        return {"stopped": False, "reason": "3000 未监听"}
    res = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                         capture_output=True, text=True, encoding="gbk", timeout=20,
                         creationflags=CREATE_NO_WINDOW)
    deadline = time.time() + 20
    while time.time() < deadline and port_open(3000):
        time.sleep(0.5)
    return {"stopped": not port_open(3000), "pid": pid, "rc": res.returncode}


def start_newapi() -> dict:
    log = open(os.path.join(NEWAPI_LOG_DIR, "newapi-restart.log"), "ab", buffering=0)
    proc = subprocess.Popen([NEWAPI_EXE, "--port", "3000", "--log-dir", NEWAPI_LOG_DIR],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            # cwd 必须与 one-api.db 同目录，否则会用空库起来（2026-10-21 踩过）
                            cwd=r"D:\wb2api\new-api", close_fds=True,
                            creationflags=CREATE_NO_WINDOW)
    deadline = time.time() + 90
    while time.time() < deadline and not port_open(3000):
        time.sleep(1)
    return {"pid": proc.pid, "listening": port_open(3000)}


def chat(model: str, timeout: float = 60) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}
    req = urllib.request.Request("http://127.0.0.1:3000/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {API_KEY}"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "ms": int((time.time() - t) * 1000),
                    "body": r.read(300).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ms": int((time.time() - t) * 1000),
                "body": e.read(300).decode("utf-8", "replace")}
    except Exception as e:
        return {"status": None, "error": str(e)[:200]}


def apply_sql() -> dict:
    import sqlite3

    con = sqlite3.connect(DB, timeout=20.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=20000")
    cur = con.cursor()
    out = {}
    cur.execute("BEGIN IMMEDIATE")
    try:
        # ① 非启用渠道：清 models（断重建源）+ 关 abilities
        chans = [r[0] for r in cur.execute(
            "select id from channels where status<>1 and exists("
            "select 1 from abilities a where a.channel_id=channels.id and a.enabled=1)").fetchall()]
        for cid in chans:
            cur.execute("UPDATE channels SET models='' WHERE id=?", (cid,))
        cur.execute(
            "UPDATE abilities SET enabled=0 WHERE channel_id IN "
            "(SELECT id FROM channels WHERE status<>1)")
        out["stripped_channels"] = chans
        out["abilities_disabled"] = cur.rowcount

        # ② 空 Key 渠道：停用 + 关 abilities
        bad = [r[0] for r in cur.execute(
            "select id from channels where status=1 and (key is null or length(trim(key))<10 "
            "or key like '%*%')").fetchall()]
        for cid in bad:
            cur.execute("UPDATE channels SET status=2 WHERE id=?", (cid,))
        if bad:
            q = ",".join("?" * len(bad))
            cur.execute(f"UPDATE abilities SET enabled=0 WHERE channel_id IN ({q})", bad)
        out["disabled_badkey_channels"] = bad

        # ③ ch25：异常状态 0 → 1（恢复兜底渠道）
        cur.execute("UPDATE channels SET status=1 WHERE id=25 AND status=0")
        out["ch25_restored"] = cur.rowcount == 1

        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bak_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "db-backup")
    os.makedirs(bak_dir, exist_ok=True)
    bak = os.path.join(bak_dir, f"one-api.db.before-stop-edit-{time.strftime('%Y%m%d-%H%M%S')}")

    if args.dry_run:
        logs.safe_print(json.dumps({"would_backup_to": bak, "would_stop": 3000,
                                    "would_start": NEWAPI_EXE}, ensure_ascii=False, indent=2))
        return 0

    shutil.copy2(DB, bak)
    logs.safe_print(f"[1/5] 备份完成：{bak}")

    stopped = stop_newapi()
    logs.safe_print(f"[2/5] 停 New API：{json.dumps(stopped, ensure_ascii=False)}")
    started = {}
    try:
        sql = apply_sql()
        logs.safe_print(f"[3/5] 写库：{json.dumps(sql, ensure_ascii=False)}")
    finally:
        started = start_newapi()
        logs.safe_print(f"[4/5] 起回 New API：{json.dumps(started, ensure_ascii=False)}")

    time.sleep(3)
    checks = {"deepseek-flash": chat("deepseek-flash"),
              "Catniti/glm-5.3-flash-free(ch90专有)": chat("Catniti/glm-5.3-flash-free", timeout=40)}
    logs.safe_print("[5/5] 路由实测：")
    for k, v in checks.items():
        body = (v.get("body") or v.get("error") or "").replace("\n", " ")[:100]
        logs.safe_print(f"      {k:<36}{v.get('status')} ({v.get('ms')}ms) {body}")

    logs.record("db.stop_edit_start", backup=bak, stopped=stopped, started=started, sql=sql, checks=checks)
    ok = started.get("listening") and checks["deepseek-flash"].get("status") == 200
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

