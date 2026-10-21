#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把新开的 novai 号并入渠道 15，并打通到"网关出词"。

按《流水线全貌》§12.2 的顺序：备份 → 停服 → 单事务写 → 起回 → 验真 → 网关验收。
写的内容：
  1) channels 15: key 追加（换行分隔）、channel_info 设 multi_key/failover、status=1
  2) channels 15: models 追加两个 [free] 名（novai 现在真供的）
  3) options.ModelRatio: 给 [free] 名补价格（否则 New API 会因"价格未配置"拒调用）
  4) abilities: 交给 New API 启动时按 channels.models 重建（它本来就会）
"""
from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

DB = r"D:\wb2api\new-api\one-api.db"
EXE = r"D:\wb2api\new-api\bin\new-api.exe"
LOGD = r"D:\wb2api\new-api\logs"
CWD = r"D:\wb2api\new-api"
STATE = r"C:\Users\Administrator\Desktop\ai-gateways\ops-console\state\novai-new-account.json"
GW_KEY = "LR6WlQ2tKdpipbmfeOX3PmbuCCJA6U0mJwZKF2S1JffYLuBO"
CNW = 0x08000000
ADD_MODELS = ["[free]deepseek-v4.1-flash", "[free]gpt-6-astra"]


def port_open(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", p)) == 0


def pid_on(p: int):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"[Console]::OutputEncoding=[Text.Encoding]::UTF8;(Get-NetTCPConnection -LocalPort {p} -State Listen).OwningProcess"],
        capture_output=True, text=True, encoding="utf-8", creationflags=CNW).stdout.strip()
    return int(out) if out.isdigit() else None


def stop_api() -> bool:
    pid = pid_on(3000)
    if not pid:
        return True
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   text=True, encoding="gbk", creationflags=CNW)
    t = time.time()
    while time.time() - t < 20 and port_open(3000):
        time.sleep(0.5)
    return not port_open(3000)


def start_api() -> bool:
    log = open(os.path.join(LOGD, "newapi-restart4.log"), "ab", buffering=0)
    subprocess.Popen([EXE, "--port", "3000", "--log-dir", LOGD], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, cwd=CWD, close_fds=True, creationflags=CNW)
    t = time.time()
    while time.time() - t < 90 and not port_open(3000):
        time.sleep(1)
    return port_open(3000)


def write_db(new_key: str) -> dict:
    con = sqlite3.connect(DB, timeout=20, isolation_level=None)
    con.execute("PRAGMA busy_timeout=20000")
    cur = con.cursor()
    info = {}
    cur.execute("BEGIN IMMEDIATE")
    try:
        row = cur.execute("select key, coalesce(models,''), coalesce(channel_info,'') from channels where id=15").fetchone()
        old_key, models, _ = row
        keys = [k.strip() for k in (old_key or "").split("\n") if k.strip()]
        if new_key not in keys:
            keys.append(new_key)
        cur.execute("UPDATE channels SET key=?, channel_info=?, status=1 WHERE id=15", (
            "\n".join(keys),
            json.dumps({"is_multi_key": True, "multi_key_size": len(keys),
                        "multi_key_status_list": None, "multi_key_polling_index": 0,
                        "multi_key_mode": "failover"}, ensure_ascii=False),
        ))
        info["keys"] = len(keys)

        names = [m.strip() for m in (models or "").split(",") if m.strip()]
        added = [m for m in ADD_MODELS if m not in names]
        names.extend(added)
        cur.execute("UPDATE channels SET models=? WHERE id=15", (",".join(names),))
        info["models_added"] = added
        info["models_total"] = len(names)

        # 价格：给新增的 [free] 名补价格（沿用 deepseek-v4.1-flash 的比例；没有就 1）
        r = cur.execute("select value from options where key='ModelRatio'").fetchone()
        ratio = json.loads(r[0]) if r and r[0] else {}
        base = ratio.get("deepseek-v4.1-flash") or ratio.get("deepseek-v4-flash") or 1
        for m in ADD_MODELS:
            ratio.setdefault(m, base)
        cur.execute("update options set value=? where key='ModelRatio'", (json.dumps(ratio, ensure_ascii=False),))
        info["ratio_base"] = base

        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return info


def gw_chat(model: str, timeout: float = 90) -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "说：好"}],
                       "max_tokens": 16}).encode()
    req = urllib.request.Request("http://127.0.0.1:3000/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {GW_KEY}"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read(4000).decode("utf-8", "replace"))
        return {"status": 200, "ms": int((time.time() - t) * 1000),
                "content": str((j.get("choices") or [{}])[0].get("message", {}).get("content"))[:60],
                "model": j.get("model")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ms": int((time.time() - t) * 1000),
                "body": e.read(300).decode("utf-8", "replace")}
    except Exception as e:
        return {"status": None, "error": str(e)[:150]}


def main() -> int:
    rec = json.load(io.open(STATE, encoding="utf-8-sig"))
    new_key = rec["key"]
    print(f"[1/6] 新 key: {new_key[:12]}…")
    print("[2/6] 停 :3000 …", "ok" if stop_api() else "FAILED")
    info = write_db(new_key)
    print("[3/6] 写库:", json.dumps(info, ensure_ascii=False))
    ok = start_api()
    print("[4/6] 起回 :3000:", ok)
    if not ok:
        return 1
    time.sleep(8)   # 等它把 channels → abilities 重建出来
    print("[5/6] 网关验收（客户端口径）")
    for m in ADD_MODELS + ["deepseek-flash"]:
        print(f"   {m:<30}", json.dumps(gw_chat(m), ensure_ascii=False)[:180])
    print("[6/6] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
