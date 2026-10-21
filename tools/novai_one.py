# -*- coding: utf-8 -*-
"""novai 补号：一次完整闭环（发码 → 读信 → 注册 → 登录 → 建 key → 验真）。

只补 1 个号；所有产物落 ops-console/state/novai-new-account.json。
遵守：发码间隔 ≥38s（本脚本自身会先等），IMAP 轮询由 2号-读信.py 控制。
"""
from __future__ import annotations

import importlib.util
import json
import random
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\ai-gateways")
STATE_NAME = "novai-new-account.json"
STATE = ROOT / "ops-console" / "state" / STATE_NAME
BASE = "https://once.novai.su"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
import os
EMAIL = os.environ.get("NOVAI_EMAIL", "3660299188@qq.com")
WAIT_VERIFY = 40          # 站点发码间隔 ≥38s
WAIT_CODE = 200

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def api(path: str, data: dict | None = None, token: str | None = None, timeout: float = 60):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    method = "GET"
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read(80000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(4000).decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": str(e)[:200]}


def load_mailer():
    spec = importlib.util.spec_from_file_location("reader", str(ROOT / "补货-2号" / "2号-读信.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print(f"[1/6] 读信助手初始化（{EMAIL}）")
    RM = load_mailer()
    mb = RM.Mailbox(addr=EMAIL)
    if not getattr(mb, "ok", False):
        print("  ✗ IMAP 登录失败，后面无法收码，终止")
        return 1
    uid0 = mb.baseline()
    print("  基线已取")

    print(f"[2/6] 等待发码间隔 {WAIT_VERIFY}s")
    time.sleep(WAIT_VERIFY)

    print("[3/6] 请求发码")
    st, resp = api("/api/verification?email=" + urllib.parse.quote(EMAIL))
    print(f"  HTTP {st} {json.dumps(resp, ensure_ascii=False)[:200]}")
    if st != 200 or (isinstance(resp, dict) and resp.get("success") is False):
        print("  ✗ 发码被拒，终止")
        return 1

    print(f"[4/6] 等验证码（最多 {WAIT_CODE}s）")
    sub, text, link, code = mb.wait_fresh(EMAIL, uid0, timeout=WAIT_CODE,
                                          expect=["novai", "once", "验证", "注册", "verif"],
                                          exclude=["重置", "reset", "password"])
    print(f"  主题={str(sub)[:80]}")
    print(f"  验证码={code}")
    if not code:
        print("  ✗ 没收到码，终止")
        return 1

    password = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    username = EMAIL.split("@")[0]
    print("[5/6] 注册")
    st, resp = api("/api/user/register", {
        "username": username, "password": password, "email": EMAIL, "verification_code": code})
    print(f"  HTTP {st} {json.dumps(resp, ensure_ascii=False)[:220]}")
    if st != 200 or (isinstance(resp, dict) and resp.get("success") is False):
        print("  ✗ 注册失败，终止")
        return 1

    print("[5b/6] 登录")
    st, resp = api("/api/user/login", {"username": username, "password": password})
    token = None
    if isinstance(resp, dict):
        token = (resp.get("data") or {}).get("access_token") or resp.get("access_token")
    print(f"  HTTP {st} token={bool(token)}")
    if not token:
        print("  ✗ 登录失败，终止")
        return 1

    st, me = api("/api/user/self", token=token)
    quota = (me.get("data") or {}).get("quota") if isinstance(me, dict) else None
    print(f"  当前余额 quota={quota}")

    print("[6/6] 建 key")
    st, resp = api("/api/token/", {"name": f"cli-{int(time.time())}", "remain_quota": 0,
                                   "expired_time": -1}, token=token)
    key = None
    if isinstance(resp, dict):
        key = (resp.get("data") or {}).get("key") or resp.get("key")
    if key and not str(key).startswith("sk-"):
        key = "sk-" + str(key)
    print(f"  HTTP {st} key={str(key)[:14]}…")

    STATE_NAME = f"novai-acct-{EMAIL.split(chr(64))[0]}.json"
    STATE = ROOT / "ops-console" / "state" / STATE_NAME
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "email": EMAIL, "username": username,
           "password": password, "token": token, "key": key, "quota": quota, "base": BASE,
           "register": resp if not key else None}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → 落盘 {STATE}")
    if not key:
        return 1

    print("[7/7] 验真：用新 key 列模型 + 打一个 [free] 模型")
    h = {"User-Agent": UA, "Authorization": "Bearer " + key, "Accept": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + "/v1/models", headers=h),
                                    timeout=40, context=ctx) as r:
            j = json.loads(r.read(200000).decode("utf-8", "replace"))
        ids = [m["id"] for m in (j.get("data") or [])]
        print(f"  模型数={len(ids)}；free={[x for x in ids if x.startswith('[free]')][:6]}")
        cand = [x for x in ids if "deepseek" in x.lower()][:3] or ids[:1]
        for model in cand:
            body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ok"}],
                               "max_tokens": 8}).encode()
            hh = dict(h); hh["Content-Type"] = "application/json"
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        BASE + "/v1/chat/completions", data=body, headers=hh, method="POST"),
                        timeout=60, context=ctx) as r2:
                    jj = json.loads(r2.read(4000).decode("utf-8", "replace"))
                print(f"  ★ {model} → 200 {str(jj.get('choices', [{}])[0].get('message', {}).get('content'))[:40]}")
            except urllib.error.HTTPError as e:
                print(f"  {model} → HTTP {e.code} {e.read(200).decode('utf-8','replace')}")
    except Exception as e:
        print("  验真失败:", str(e)[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())
