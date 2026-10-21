"""补号流水线 —— 11 阶段状态机（断点续做 + 闸门 + 配额）。

阶段安全分级：
- 只读/可逆：probe_site / prepare_email / validate_key / smoke_test
- 破坏性：update_channel / rebuild_abilities（改渠道 / 改 abilities 表）
  这两个在控制台里默认 **blocked**，只输出「该谁做、用什么命令做」，
  避免在没接上数据库事务与 New API 停机保障前动生产。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import atomic, config, logs, proc, ratelimit, state

CKPT = config.STATE_DIR / "restock.json"
ACCOUNTS = config.STATE_DIR / "accounts.jsonl"

STAGES: list[dict] = [
    {"id": "probe_site", "kind": "read", "desc": "探测目标站可达性 + 在线模型清单"},
    {"id": "prepare_email", "kind": "read", "desc": "校验邮箱域名白名单与可用额度"},
    {"id": "request_code", "kind": "write-remote", "desc": "GET /api/verification 发码（≥38s）"},
    {"id": "read_code", "kind": "read", "desc": "IMAP 扫 INBOX+Junk 取 6 位码"},
    {"id": "register", "kind": "write-remote", "desc": "POST /api/user/register"},
    {"id": "login", "kind": "write-remote", "desc": "POST /api/user/login → access_token"},
    {"id": "create_key", "kind": "write-remote", "desc": "POST /api/token/ 建 key"},
    {"id": "collect_keys", "kind": "local", "desc": "汇总去重，落 state/keys-pending.json"},
    {"id": "validate_key", "kind": "read", "desc": "直连上游打目标模型，判定真伪"},
    {"id": "update_channel", "kind": "destructive", "desc": "换行拼接 key + failover 写回渠道 15"},
    {"id": "rebuild_abilities", "kind": "destructive", "desc": "na_abilities.py --only 15（★ 漏了不生效）"},
    {"id": "smoke_test", "kind": "read", "desc": "na_test15.py 实测 7 个 [free] 模型"},
]
STAGE_IDS = [s["id"] for s in STAGES]


def _ckpt() -> dict:
    return atomic.read_json(CKPT, default={}) or {}


def _save_ckpt(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic.write_json(CKPT, data)


def _target() -> dict:
    return (config.load_policies().get("restock") or {}).get("target") or {}


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
          timeout: float = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0", **(headers or {})})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4000).decode("utf-8", "replace")
            return {"status": resp.status, "ms": int((time.time() - started) * 1000), "body": raw[:800]}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "ms": int((time.time() - started) * 1000),
                "body": exc.read(400).decode("utf-8", "replace")[:400]}
    except Exception as exc:
        return {"status": None, "error": str(exc)[:200], "ms": int((time.time() - started) * 1000)}


# ── 配额 ────────────────────────────────────────────────────────────────
def _accounts_today() -> int:
    if not ACCOUNTS.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    n = 0
    for line in ACCOUNTS.read_text(encoding="utf-8-sig").splitlines():
        try:
            if json.loads(line).get("at", "").startswith(today):
                n += 1
        except json.JSONDecodeError:
            continue
    return n


def quota() -> dict:
    q = (config.load_policies().get("restock") or {}).get("quota") or {}
    used = _accounts_today()
    per_run = int(q.get("max_accounts_per_run") or 3)
    per_day = int(q.get("max_accounts_per_day") or 10)
    return {"today": used, "per_day": per_day, "per_run": per_run,
            "remaining_today": max(0, per_day - used), "ok": used < per_day}


# ── 阶段实现 ─────────────────────────────────────────────────────────────
def _stage_probe_site() -> dict:
    base = (_target().get("base_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "policies.restock.target.base_url 未配置"}
    gate = ratelimit.acquire("site_probe")
    if not gate["ok"]:
        return {"ok": False, "rate_limited": gate}
    attempts = []
    for path in ("/api/status", "/api/pricing"):
        r = _http("GET", base + path)
        attempts.append({"path": path, **{k: r.get(k) for k in ("status", "ms", "error")}})
        if r.get("status") == 200:
            models = 0
            try:
                payload = json.loads(r["body"]) if r.get("body", "").startswith(("{", "[")) else None
                if isinstance(payload, dict):
                    for key in ("data", "models", "pricing"):
                        v = payload.get(key)
                        if isinstance(v, (list, dict)):
                            models = len(v)
                            break
            except json.JSONDecodeError:
                models = 0
            return {"ok": True, "attempts": attempts, "models_hint": models}
    return {"ok": False, "attempts": attempts}


def _stage_prepare_email() -> dict:
    cfg = (config.load_policies().get("restock") or {}).get("email") or {}
    domains = cfg.get("whitelist_domains") or []
    if not domains:
        return {"ok": False, "error": "email.whitelist_domains 未配置"}
    return {"ok": True, "whitelist": domains, "imap": f"{cfg.get('imap_host')}:{cfg.get('imap_port')}",
            "min_interval_sec": cfg.get("poll_interval_sec_min"),
            "note": "邮箱地址需要人工/既有邮箱池提供；控制台只校验域名合法性，不代为注册邮箱"}


def find_keys(obj: Any, out: list[str] | None = None) -> list[str]:
    out = out if out is not None else []
    if isinstance(obj, dict):
        for v in obj.values():
            find_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_keys(v, out)
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith("sk-") and 12 <= len(s) <= 200:
            out.append(s)
    return out


def _stage_validate_key(args: dict) -> dict:
    base = (args.get("base_url") or _target().get("base_url") or "").rstrip("/")
    model = args.get("model") or "deepseek-v4.1-flash"
    keys = list(args.get("keys") or [])
    if not keys and args.get("farm_file"):
        path = config.resolve(args["farm_file"])
        if not path.exists():
            return {"ok": False, "error": f"farm 文件不存在：{path}"}
        keys = find_keys(atomic.read_json(path, default={}))
    keys = list(dict.fromkeys(keys))[: int(args.get("limit") or 3)]
    if not keys:
        return {"ok": False, "error": "没有可验证的 key"}
    gate = ratelimit.acquire("key_validate")
    if not gate["ok"]:
        return {"ok": False, "rate_limited": gate}
    results = []
    for k in keys:
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}
        r = _http("POST", base + "/v1/chat/completions", body=body,
                  headers={"Authorization": f"Bearer {k}"}, timeout=30)
        cls = {200: "good", 400: "invalid-model", 401: "invalid-key", 403: "no-quota",
               429: "rate-limited", 503: "no-model-upstream"}.get(r.get("status"), "unknown")
        results.append({"key": k[:10] + "…", "status": r.get("status"), "class": cls, "ms": r.get("ms")})
    ok = any(x["class"] == "good" for x in results)
    return {"ok": ok, "model": model, "results": results}


DESTRUCTIVE_HINTS = {
    "update_channel": "需人工/后续 P3：停 New API → UPDATE channels SET key='<换行拼接>', "
                      "channel_info='{\"is_multi_key\":true,\"multi_key_mode\":\"failover\"}' WHERE id=15 → 起回",
    "rebuild_abilities": "需人工/后续 P3：python na_abilities.py --only 15（★ 不跑则新 key 不生效）",
}


def run_stage(stage: str, args: dict | None = None, apply: bool = False) -> dict:
    args = args or {}
    if stage not in STAGE_IDS:
        return {"ok": False, "error": f"未知阶段 {stage}", "stages": STAGE_IDS}
    meta = next(s for s in STAGES if s["id"] == stage)
    ck = _ckpt()

    if meta["kind"] == "destructive":
        return {"ok": False, "blocked": True, "stage": stage,
                "reason": "破坏性阶段默认拦截（未接数据库事务与停机保障）",
                "manual": DESTRUCTIVE_HINTS.get(stage, "")}

    if stage == "probe_site":
        result = _stage_probe_site()
    elif stage == "prepare_email":
        result = _stage_prepare_email()
    elif stage == "validate_key":
        result = _stage_validate_key(args)
    else:
        return {"ok": False, "blocked": True, "stage": stage,
                "reason": "该阶段需要站点/IMAP 交互，尚未接入（P2 后续）",
                "implemented": ["probe_site", "prepare_email", "validate_key"]}

    ck.setdefault("results", {})[stage] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "ok": bool(result.get("ok")), "detail": result}
    ck["stage"] = stage
    _save_ckpt(ck)
    logs.record("pipeline.stage", stage=stage, ok=bool(result.get("ok")), apply=apply)
    return {"ok": bool(result.get("ok")), "stage": stage, **result}


def plan() -> dict:
    q = quota()
    gate = ratelimit.wait_time("email_verification")
    ck = _ckpt()
    return {
        "target": _target(),
        "quota": q,
        "email_gate": gate,
        "checkpoint": {"stage": ck.get("stage"), "updated_at": ck.get("updated_at"),
                       "stages_done": sorted((ck.get("results") or {}).keys())},
        "next": _next_stage(ck),
        "stages": STAGES,
        "blocked_stages": [s["id"] for s in STAGES if s["kind"] == "destructive"],
    }


def _next_stage(ck: dict) -> str | None:
    done = (ck.get("results") or {})
    for s in STAGES:
        if s["id"] not in done and s["kind"] != "destructive":
            return s["id"]
    return None
