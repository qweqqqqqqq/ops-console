#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""novai 多账号同步：把 state/novai-acct-*.json 里所有账号的 key 并进渠道 15。

流程（每次跑都幂等）：
  1) 对每个账号文件：缺 key 就用它的 access_token 找「初始令牌」并取明文
  2) 与 ch15 现有 key 去重合并（换行分隔，failover）
  3) 停 :3000 → 写 channels → 跑 na_abilities --only 15 → 起 :3000
  4) 客户端口径验收
"""
from __future__ import annotations

import glob
import io
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\ai-gateways")
STATE_DIR = ROOT / "ops-console" / "state"
DB = r"D:\wb2api\new-api\one-api.db"
EXE = r"D:\wb2api\new-api\bin\new-api.exe"
LOGD = r"D:\wb2api\new-api\logs"
CWD = r"D:\wb2api\new-api"
BASE = "https://once.novai.su"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36"
GW_KEY = "LR6WlQ2tKdpipbmfeOX3PmbuCCJA6U0mJwZKF2S1JffYLuBO"
CNW = 0x08000000
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE


def api(path: str, token: str | None = None, data: dict | None = None, timeout: float = 45):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = None
    method = "GET"
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read(300000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read(300).decode("utf-8", "replace")}
    except Exception as e:
        return None, {"error": str(e)[:150]}


def ensure_key(rec: dict) -> str | None:
    if rec.get("key"):
        return rec["key"]
    tok = rec.get("token")
    if not tok:
        return None
    st, lst = api("/api/token/?p=0&size=20", token=tok)
    items = ((lst.get("data") or {}).get("items") or []) if isinstance(lst, dict) else []
    pick = None
    for it in items:
        if it.get("unlimited_quota") or "初始" in str(it.get("name", "")):
            pick = it
            break
    if not pick and items:
        pick = items[0]
    if not pick:
        return None
    st, kk = api(f"/api/token/{pick['id']}/key", token=tok, data={})
    key = ((kk.get("data") or {}).get("key")) if isinstance(kk, dict) else None
    if not key:
        return None
    key = key if str(key).startswith("sk-") else "sk-" + str(key)
    rec["key"], rec["token_id"] = key, pick["id"]
    return key


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
    log = open(os.path.join(LOGD, "newapi-sync.log"), "ab", buffering=0)
    subprocess.Popen([EXE, "--port", "3000", "--log-dir", LOGD], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, cwd=CWD, close_fds=True, creationflags=CNW)
    t = time.time()
    while time.time() - t < 90 and not port_open(3000):
        time.sleep(1)
    return port_open(3000)


def merge_keys(keys: list[str]) -> dict:
    con = sqlite3.connect(DB, timeout=20, isolation_level=None)
    con.execute("PRAGMA busy_timeout=20000")
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        row = cur.execute("select key from channels where id=15").fetchone()
        cur_keys = [k.strip() for k in (row[0] or "").split("\n") if k.strip()]
        added = [k for k in keys if k not in cur_keys]
        final = cur_keys + added
        cur.execute("UPDATE channels SET key=?, channel_info=?, status=1 WHERE id=15", (
            "\n".join(final),
            json.dumps({"is_multi_key": True, "multi_key_size": len(final),
                        "multi_key_status_list": None, "multi_key_polling_index": 0,
                        "multi_key_mode": "failover"}, ensure_ascii=False),
        ))
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"before": len(cur_keys), "added": len(added), "total": len(final)}


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
                "content": str((j.get("choices") or [{}])[0].get("message", {}).get("content"))[:30]}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ms": int((time.time() - t) * 1000),
                "body": e.read(200).decode("utf-8", "replace")}
    except Exception as e:
        return {"status": None, "error": str(e)[:120]}


def main() -> int:
    files = sorted(glob.glob(str(STATE_DIR / "novai-acct-*.json"))) + [str(STATE_DIR / "novai-new-account.json")]
    keys, report = [], []
    for f in files:
        if not os.path.exists(f):
            continue
        rec = json.load(io.open(f, encoding="utf-8-sig"))
        k = ensure_key(rec)
        io.open(f, "w", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False, indent=2))
        report.append({"file": os.path.basename(f), "email": rec.get("email"), "key": (k or "")[:12]})
        if k:
            keys.append(k)
    keys = list(dict.fromkeys(keys))
    print("账号:", json.dumps(report, ensure_ascii=False))
    if not keys:
        print("没有任何可用 key")
        return 1

    print("停 :3000 …", "ok" if stop_api() else "FAILED")
    info = merge_keys(keys)
    print("合并:", json.dumps(info, ensure_ascii=False))
    print("重建 abilities …")
    r = subprocess.run([sys.executable, str(ROOT / "na_abilities.py"), "--only", "15"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(ROOT), creationflags=CNW)
    tail = [l for l in (r.stdout or "").splitlines() if "行" in l or "★" in l][-3:]
    print("  ", " | ".join(tail))
    print("起 :3000 …", "ok" if start_api() else "FAILED")
    time.sleep(8)
    print("验收:")
    for m in ["[free]deepseek-v4.1-flash", "[free]gpt-6-astra", "claude-sonnet-5-5", "deepseek-flash"]:
        print(f"   {m:<30}", json.dumps(gw_chat(m), ensure_ascii=False)[:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())
