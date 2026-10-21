#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补号 / 中转栈控制台 —— 一键管理的唯一入口。

用法（全部只读安全，除 --json/--no-http 外无副作用）：
    python manage.py status            # 全景：服务 / 守护 / 任务 / 产线
    python manage.py status --json     # 机器可读（供 DSH 工具消费）
    python manage.py doctor            # 12 条硬规矩自动体检
    python manage.py lines             # 6 条补号产线地图
    python manage.py tasks             # 相关计划任务
    python manage.py daemons           # 常驻进程
    python manage.py ports             # 端口占用与归属
    python manage.py log --tail 20     # 最近决策日志

设计见 docs/DESIGN.md。执行类动作（start/stop/restock）在 P1/P2 阶段接入，
且必须过 policies.json 的 dry-run + confirm token。
"""
from __future__ import annotations

import argparse
import json
import sys

from console import actions, checks, config, logs, pipeline, ratelimit, state


def _strip_internal(obj):
    if isinstance(obj, dict):
        return {k: _strip_internal(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_internal(x) for x in obj]
    return obj


def cmd_status(args) -> int:
    snap = state.snapshot(probe_http=not args.no_http)
    if args.json:
        logs.safe_print(json.dumps(_strip_internal(snap), ensure_ascii=False, indent=2))
        return 0

    c = snap["counts"]
    logs.safe_print(f"补号 / 中转栈控制台  v{__import__('console').__version__}")
    logs.safe_print(f"时间 {snap['generated_at']}   目录 {config.ROOT}")
    logs.safe_print(f"服务 {c['listening']}/{c['services']} 在监听（我们可管 {c['managed']}，禁区 {c['forbidden']}）"
                    f"   相关进程 {c['processes']}   疑似守护 {c['daemons']}   相关任务 {len(snap['tasks'])}")
    logs.safe_print("")
    logs.safe_print(f"{'服务':<18}{'端口':<7}{'状态':<8}{'HTTP':<7}{'策略':<10}备注")
    logs.safe_print("-" * 92)
    for s in snap["services"]:
        flag = "禁区" if s["forbidden"] else ("可管" if s["managed_by_us"] else "只观测")
        http = s.get("http", {}).get("status") if s.get("http") else ""
        st = "LISTEN" if s["listening"] else "-"
        note = (s.get("holder") or "")[:42]
        logs.safe_print(f"{str(s['name'])[:16]:<18}{s['port']:<7}{st:<8}{str(http or ''):<7}{flag:<10}{note}")
    if snap["findings"]:
        logs.safe_print("")
        logs.safe_print("发现：")
        for f in snap["findings"]:
            logs.safe_print(f"  [{f['level']:<5}] {f['msg']}")
    return 0


def cmd_doctor(args) -> int:
    snap = state.snapshot(probe_http=False)
    results = checks.run_all(snap)
    if args.json:
        logs.safe_print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    bad = [r for r in results if not r["ok"]]
    logs.safe_print(f"体检 {len(results)} 项，异常 {len(bad)} 项")
    logs.safe_print("-" * 92)
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        logs.safe_print(f"{mark} {r['id']:<4}{r['severity']:<6}{r['detail']}")
    return 1 if any(r["severity"] == "error" and not r["ok"] for r in results) else 0


def cmd_lines(args) -> int:
    data = config.load_json(config.CONFIG_DIR / "lines.json") or {}
    if args.json:
        logs.safe_print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    logs.safe_print("6 条补号产线")
    logs.safe_print("-" * 92)
    for ln in data.get("lines", []):
        ports = f" 端口 {ln['ports']}" if ln.get("ports") else ""
        logs.safe_print(f"{ln['name']:<10}{ln['role']:<18}{ln['dir']}{ports}")
        logs.safe_print(f"          {ln['summary']}")
    shared = data.get("shared_pipeline") or {}
    logs.safe_print("")
    logs.safe_print("共用流水线")
    for k, v in shared.items():
        logs.safe_print(f"  {k:<18}{v.get('script') or ''} {v.get('status') or v.get('interval_min') or ''}")
    return 0


def cmd_tasks(args) -> int:
    rows = state.scheduled_tasks()
    if args.json:
        logs.safe_print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    logs.safe_print(f"相关计划任务 {len(rows)} 条")
    for r in rows:
        logs.safe_print(f"  {r['status']:<10}{r['name']}")
    return 0


def cmd_daemons(args) -> int:
    snap = state.snapshot(probe_http=False)
    rows = snap["daemons"]
    if args.json:
        logs.safe_print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    logs.safe_print(f"疑似常驻守护 {len(rows)} 个")
    for r in rows:
        logs.safe_print(f"  pid {str(r['pid']):<7}{r['name']:<12}{(r['cmd'] or '')[:110]}")
    return 0


def cmd_ports(args) -> int:
    ports = state.listening_ports()
    cfg = config.load_services()
    reserved = {int(r["port"]): r for r in (cfg.get("reserved_ports") or [])}
    procs = {p["pid"]: p for p in state.process_index()}
    rows = sorted(ports.items())
    logs.safe_print(f"监听端口 {len(rows)} 个")
    for port, pid in rows:
        tag = "保留" if port in reserved else ""
        owner = reserved.get(port, {}).get("owner", "")
        cmd = (procs.get(pid) or {}).get("cmd", "")
        logs.safe_print(f"  {port:<7}pid {pid:<7}{tag:<5}{owner:<38}{cmd[:60]}")
    return 0


def cmd_log(args) -> int:
    path = config.LOG_DIR / ((config.load_policies().get("safety") or {}).get("decision_log", "decisions.jsonl"))
    if not path.exists():
        logs.safe_print("暂无决策日志")
        return 0
    lines = path.read_text(encoding="utf-8-sig").splitlines()[-args.tail:]
    for line in lines:
        logs.safe_print(line)
    return 0


def _action_cmd(kind):
    def handler(args):
        fn = getattr(actions, kind)
        try:
            res = fn(args.key, apply=args.apply)
        except actions.Refused as exc:
            logs.safe_print(f"[拒绝] {exc}")
            logs.record(f"service.{kind}.refused", key=args.key, reason=str(exc))
            return 2
        logs.safe_print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    return handler


def cmd_pipeline(args) -> int:
    if args.pipe_cmd == "plan":
        out = pipeline.plan()
    elif args.pipe_cmd == "status":
        out = {"checkpoint": pipeline._ckpt(), "quota": pipeline.quota(),
               "gates": ratelimit.snapshot()}
    else:  # run
        out = pipeline.run_stage(args.stage, {
            "farm_file": args.farm_file,
            "base_url": args.base_url,
            "keys": args.key or [],
            "model": args.model,
            "limit": args.limit,
        }, apply=args.apply)
    logs.safe_print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ratelimit(args) -> int:
    logs.safe_print(json.dumps(ratelimit.snapshot(), ensure_ascii=False, indent=2))
    return 0


def cmd_db(args) -> int:
    from console import newapi_db

    if args.fix:
        if args.fix == "residual":
            out = newapi_db.fix_residual(apply=args.apply)
        elif args.fix == "strip":
            out = newapi_db.strip_disabled_channels(apply=args.apply)
        elif args.fix == "badkeys":
            out = newapi_db.fix_badkeys(apply=args.apply)
        elif args.fix == "enable25":
            out = newapi_db.set_channel_status(25, 1, apply=args.apply, reason="status=0 异常值，恢复启用")
        elif args.fix == "rollback":
            if not args.rollback_file:
                logs.safe_print("需要 --rollback-file <路径>")
                return 2
            out = newapi_db.rollback(args.rollback_file, apply=args.apply)
        else:
            logs.safe_print(f"未知修复项：{args.fix}")
            return 2
        logs.safe_print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    out = newapi_db.summary() if args.summary else newapi_db.audit()
    if args.summary and not args.json:
        for k, v in out.items():
            logs.safe_print(f"{k:<18}{v}")
        return 0 if out.get("ok") else 1
    if args.json:
        logs.safe_print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    logs.safe_print(f"New API 库审计  {out.get('path')}")
    if not out.get("ok"):
        logs.safe_print(f"[不可用] {out.get('error')}")
        return 1
    if not out["findings"]:
        logs.safe_print("未发现问题")
    for f in out["findings"]:
        logs.safe_print(f"  [{f['level']:<5}] {f['code']:<22}{f['detail']}")
    logs.safe_print(f"单点路由总数：{out['stats'].get('single_point_total')}")
    if args.matrix:
        logs.safe_print("\n路由样例（group / model / channel / priority）")
        for row in (out.get("matrix_sample") or [])[:25]:
            logs.safe_print(f"  {row['group']:<12}{row['model']:<26}ch{row['channel']:<7}pri={row['priority']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="manage.py", description="补号 / 中转栈一键控制台")
    ap.add_argument("--version", action="version", version=f"ops-console {__import__('console').__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="全景状态")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-http", action="store_true", help="跳过 HTTP 探活（更快）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="硬规矩自动体检")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("lines", help="6 条补号产线")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lines)

    p = sub.add_parser("tasks", help="相关计划任务")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("daemons", help="常驻守护进程")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_daemons)

    p = sub.add_parser("ports", help="端口占用")
    p.set_defaults(func=cmd_ports)

    p = sub.add_parser("log", help="决策日志")
    p.add_argument("--tail", type=int, default=20)
    p.set_defaults(func=cmd_log)

    for kind, help_text in (("start", "启动白名单服务（默认 dry-run）"),
                            ("stop", "停止白名单服务（需确认令牌）"),
                            ("restart", "重启白名单服务")):
        p = sub.add_parser(kind, help=help_text)
        p.add_argument("key", help="config/services.json 里的 key，如 cf_adapter")
        p.add_argument("--apply", action="store_true", help="真正执行（否则只打印计划）")
        p.set_defaults(func=_action_cmd(kind))

    p = sub.add_parser("pipeline", help="补号流水线")
    psub = p.add_subparsers(dest="pipe_cmd", required=True)
    psub.add_parser("plan", help="下一步该做什么 + 配额/闸门").set_defaults(func=cmd_pipeline)
    psub.add_parser("status", help="断点与闸门现状").set_defaults(func=cmd_pipeline)
    pr = psub.add_parser("run", help="执行某个阶段")
    pr.add_argument("stage", help="阶段 id，如 probe_site / prepare_email / validate_key")
    pr.add_argument("--apply", action="store_true")
    pr.add_argument("--farm-file", help="从 farm-*.json 里取 key")
    pr.add_argument("--key", action="append", help="直接指定 key（可多次）")
    pr.add_argument("--base-url", help="覆盖目标站 base url")
    pr.add_argument("--model", help="验证用的模型名")
    pr.add_argument("--limit", type=int, default=3)
    pr.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("ratelimit", help="全局频率闸门现状")
    p.set_defaults(func=cmd_ratelimit)

    p = sub.add_parser("db", help="New API 库审计（只读）/ 修复（可回滚）")
    p.add_argument("--summary", action="store_true", help="只看总量：渠道/abilities/分组/模型")
    p.add_argument("--matrix", action="store_true", help="附路由样例（group/model/channel/priority）")
    p.add_argument("--fix", choices=["residual", "strip", "badkeys", "enable25", "rollback"],
                   help="修复项：residual=关掉残留 abilities / badkeys=停空 Key 渠道 / enable25=恢复 ch25 / rollback=按文件回滚")
    p.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    p.add_argument("--rollback-file", help="rollback 用的回滚文件")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_db)

    args = ap.parse_args(argv)
    rc = args.func(args)
    if args.cmd in ("status", "doctor", "ports"):
        logs.record(args.cmd, argv=sys.argv[1:], rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
