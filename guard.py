#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补号系统巡检守护 —— 由计划任务每小时静默调用。

职责（只读为主）：
  1. 库审计：残留 abilities / 孤儿 / 空 Key / group 逗号
  2. 进程：重复实例
  3. 端口：保留端口归属
发现漂移时：
  - 默认只报告（写 logs/guard.log + decisions.jsonl）
  - policies.safety.auto_strip = true 时，执行「截断残留」（不重启服务，遇到回滚风险会记录）

pythonw 友好：不依赖 stdout（sys.stdout 可能是 None），一律写文件。
退出码恒为 0（避免计划任务显示失败）。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from console import config, logs, newapi_db, state  # noqa: E402

LOG = config.LOG_DIR / "guard.log"
DECISIONS = config.LOG_DIR / "decisions.jsonl"
INGEST_STATE = config.STATE_DIR / "guard-ingest.json"
ULTRON = "http://127.0.0.1:9999"


def _http_json(method: str, url: str, payload: dict | None = None, token: str | None = None,
               timeout: float = 120) -> dict:
    import urllib.request

    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    hdr = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ingest_decisions() -> dict:
    """把上次巡检之后的新决策，沉淀进 Ultron（这就是"Agent 在学东西"的那一步）。

    为什么放这里：不依赖任何会话的工具面，也不阻塞控制台动作；
    每小时一次，把 service.* / db.* / pipeline.* 的真实操作变成可复用的经验。
    """
    if not DECISIONS.exists():
        return {"ok": True, "skipped": "no decisions"}
    state = atomic_read(INGEST_STATE) or {}
    last_ts = state.get("last_ts") or ""
    fresh: list[dict] = []
    for line in DECISIONS.read_text(encoding="utf-8-sig").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (e.get("ts") or "") > last_ts:
            fresh.append(e)
    # 只沉淀"做过事"的条目，过滤 status/doctor 这类观测
    interesting = [e for e in fresh if str(e.get("action", "")).startswith(("service.", "db.", "pipeline."))]
    if not interesting:
        _write_json(INGEST_STATE, {"last_ts": max((e.get("ts", "") for e in fresh), default=last_ts)})
        return {"ok": True, "ingested": 0, "reason": "no actionable decisions"}

    try:
        login = _http_json("POST", ULTRON + "/auth/login",
                           {"username": config.secret("ultron_user") or "labuser",
                            "password": config.secret("ultron_password")})
        token = login["data"]["token"]
    except Exception as exc:
        return {"ok": False, "error": f"ultron login failed: {exc}"[:200]}

    # 合成至多 2 条文本（每次 ingest 要跑一轮 LLM 抽取，不宜太多）
    chunks = []
    lines = []
    for e in interesting[-12:]:
        act = e.get("action")
        detail = {k: v for k, v in e.items() if k not in ("ts", "pid", "action")}
        lines.append(f"{act}: {json.dumps(detail, ensure_ascii=False)[:300]}")
    chunks.append("补号系统自动巡检：以下是最近的真实操作记录。\n" + "\n".join(lines))

    results = []
    for text in chunks:
        try:
            r = _http_json("POST", ULTRON + "/ingest/text",
                           {"text": text, "agent_id": "default_agent"}, token=token)
            results.append({"ok": True,
                            "extracted": len(((r.get("data") or {}).get("memories")) or [])})
        except Exception as exc:
            results.append({"ok": False, "error": str(exc)[:200]})
    _write_json(INGEST_STATE, {"last_ts": max((e.get("ts", "") for e in fresh), default=last_ts),
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return {"ok": all(r.get("ok") for r in results), "results": results, "lines": len(lines)}


def atomic_read(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _write(text: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


def main() -> int:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    report: dict = {"at": ts, "drift": [], "fixed": None}

    audit = newapi_db.audit()
    residual = 0
    if audit.get("ok"):
        for f in audit.get("findings", []):
            if f["level"] == "error":
                report["drift"].append({"code": f["code"], "detail": f["detail"]})
        try:
            import sqlite3

            con = sqlite3.connect(f"file:{newapi_db.db_path()}?mode=ro", uri=True, timeout=8)
            residual = con.execute(
                "select count(*) from abilities a left join channels c on c.id=a.channel_id "
                "where a.enabled=1 and (c.id is null or c.status<>1)").fetchone()[0]
            con.close()
        except Exception as exc:
            report["drift"].append({"code": "db-read", "detail": str(exc)[:150]})
    else:
        report["drift"].append({"code": "db-unavailable", "detail": audit.get("error", "")[:150]})
    report["residual"] = residual

    try:
        snap = state.snapshot(probe_http=False)
        dups = [d for d in snap.get("daemons", []) if d.get("cmd")]
        # 只报重复：按脚本名分组 >1
        import re
        from collections import Counter

        names = []
        for d in dups:
            m = re.search(r"([\w\-\u4e00-\u9fff]+\.(?:py|bat|cmd|vbs))", d["cmd"])
            if m:
                names.append(m.group(1))
        for name, n in Counter(names).items():
            if n > 1:
                report["drift"].append({"code": "dup-instance", "detail": f"{name}×{n}"})
    except Exception as exc:
        report["drift"].append({"code": "snapshot", "detail": str(exc)[:150]})

    auto = bool((config.load_policies().get("safety") or {}).get("auto_strip"))
    if residual > 0 and auto:
        try:
            report["fixed"] = newapi_db.strip_disabled_channels(apply=True)
        except Exception as exc:
            report["fixed"] = {"ok": False, "error": str(exc)[:200]}

    line = (f"[{ts}] residual={residual} drift={len(report['drift'])} "
            f"{'auto_strip=' + str(report['fixed'] is not None) if auto else 'report-only'}")
    _write(line)
    for d in report["drift"]:
        _write(f"    · {d['code']}: {d['detail'][:200]}")

    # 把最近的实操决策沉淀进 Ultron（失败不影响巡检）
    try:
        ing = ingest_decisions()
        report["ultron_ingest"] = ing
        _write(f"    ultron_ingest: {json.dumps(ing, ensure_ascii=False)[:200]}")
    except Exception as exc:
        report["ultron_ingest"] = {"ok": False, "error": str(exc)[:200]}
        _write(f"    ultron_ingest FAILED: {exc}")

    try:
        (config.STATE_DIR / "guard-last.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    logs.record("guard", **{k: v for k, v in report.items() if k != "at"}, at=ts)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 守护不能把计划任务变成"失败"
        _write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FATAL {exc}")
        sys.exit(0)
