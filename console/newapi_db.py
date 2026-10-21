"""New API 数据库审计（只读）—— 把《流水线全貌》里的硬规则变成可执行检查。

依据：补货-6号/流水线全貌-2026-10-21.md §12.3 / §14
  · abilities.group 精确匹配，逗号拼接 = 永远路由不到（卡了 3 天）
  · 按 (channel, model) 去重会删掉 group 维度 ⇒ 某分组静默失联
  · priority 数值越大越优先（0 最低）
  · 停渠道要同时停 abilities，否则残留路由照样发请求
  · channels.models 只是声明，不代表上游供货

安全：一律 mode=ro 只读打开；不写任何东西；SQLITE_BUSY 时重试并如实报告。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import atomic, config, logs

DEFAULT_DB = r"D:\wb2api\new-api\one-api.db"


def db_path() -> str:
    return (config.load_services().get("newapi_db") or {}).get("path") or DEFAULT_DB


# ── 写路径（全部先备份 + 单事务 + 可回滚）────────────────────────────────
def backup(tag: str = "manual") -> str:
    """按《流水线全貌》§12.2 第一步：改库前先备份。"""
    import shutil

    src = db_path()
    dst_dir = config.STATE_DIR / "db-backup"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"one-api.db.before-{tag}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, dst)
    return str(dst)


def _write_conn() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=20.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=20000")
    return con


def _apply_changes(con: sqlite3.Connection, changes: list[dict]) -> None:
    """changes: [{table, where:{col:val}, field, before, after}]，单事务执行。

    注意：abilities 没有 id 列，主键是 (group, model, channel_id) 组合，所以一律用 where 定位。
    """
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        for ch in changes:
            where = ch["where"]
            clause = " AND ".join(f"`{k}`=?" for k in where)
            cur.execute(
                f"UPDATE {ch['table']} SET `{ch['field']}`=? WHERE {clause}",
                [ch["after"], *where.values()])
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def _save_rollback(changes: list[dict], tag: str) -> str:
    path = config.STATE_DIR / f"db-rollback-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    atomic.write_json(path, {"db": db_path(), "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "changes": changes})
    return str(path)


def fix_residual(apply: bool = False) -> dict:
    """把"渠道非启用/不存在"却仍 enabled=1 的 abilities 关掉（可逆，不删除）。"""
    path = db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    try:
        rows = con.execute(
            "select a.`group`, a.model, a.channel_id from abilities a "
            "left join channels c on c.id=a.channel_id "
            "where a.enabled=1 and (c.id is null or c.status<>1)").fetchall()
    finally:
        con.close()
    plan = {"action": "db.fix_residual", "to_disable": len(rows), "by_channel": {}}
    for _g, _m, cid in rows:
        plan["by_channel"][str(cid)] = plan["by_channel"].get(str(cid), 0) + 1
    if not apply:
        return {"ok": True, "dry_run": True, **plan}
    if not rows:
        return {"ok": True, "skipped": True, "reason": "没有需要处理的残留", **plan}

    bak = backup("fix-residual")
    changes = [{"table": "abilities",
                "where": {"group": g, "model": m, "channel_id": cid},
                "field": "enabled", "before": 1, "after": 0}
               for g, m, cid in rows]
    wcon = _write_conn()
    try:
        _apply_changes(wcon, changes)
    finally:
        wcon.close()
    rb = _save_rollback(changes, "fix-residual")
    logs.record("db.fix_residual", count=len(rows), backup=bak, rollback=rb)
    return {"ok": True, "disabled": len(rows), "backup": bak, "rollback_file": rb, **plan}


def fix_badkeys(apply: bool = False) -> dict:
    """启用渠道但 Key 空/掩码/过短 → 停用渠道并关掉其 abilities（可逆）。"""
    path = db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    try:
        bad = con.execute(
            "select id, name, length(key) from channels where status=1 "
            "and (key is null or length(trim(key))<10 or key like '%*%')").fetchall()
        rows = con.execute(
            "select a.`group`, a.model, a.channel_id from abilities a where a.enabled=1 and a.channel_id in "
            "(select id from channels where status=1 and (key is null or length(trim(key))<10 or key like '%*%'))"
        ).fetchall()
    finally:
        con.close()
    plan = {"action": "db.fix_badkeys", "channels": [{"id": c, "name": n, "key_len": ln} for c, n, ln in bad],
            "abilities_to_disable": len(rows)}
    if not apply:
        return {"ok": True, "dry_run": True, **plan}
    if not bad:
        return {"ok": True, "skipped": True, "reason": "没有空 Key 渠道", **plan}

    bak = backup("fix-badkeys")
    changes = [{"table": "channels", "where": {"id": c}, "field": "status", "before": 1, "after": 2}
               for c, _n, _ln in bad]
    changes += [{"table": "abilities",
                 "where": {"group": g, "model": m, "channel_id": cid},
                 "field": "enabled", "before": 1, "after": 0}
                for g, m, cid in rows]
    wcon = _write_conn()
    try:
        _apply_changes(wcon, changes)
    finally:
        wcon.close()
    rb = _save_rollback(changes, "fix-badkeys")
    logs.record("db.fix_badkeys", channels=[c for c, _, _ in bad], abilities=len(rows), backup=bak, rollback=rb)
    return {"ok": True, "channels_disabled": len(bad), "abilities_disabled": len(rows),
            "backup": bak, "rollback_file": rb, **plan}


def set_channel_status(channel_id: int, status: int, apply: bool = False, reason: str = "") -> dict:
    """改单个渠道状态（如 ch25 从 0 → 1）。可逆。"""
    path = db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    try:
        row = con.execute("select id, name, status, priority, length(key) from channels where id=?",
                          (channel_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": f"渠道 {channel_id} 不存在"}
    cid, name, current, pri, klen = row
    plan = {"action": "db.set_channel_status", "channel": {"id": cid, "name": name,
                                                           "status_before": current, "status_after": status,
                                                           "priority": pri, "key_len": klen},
            "reason": reason}
    if current == status:
        return {"ok": True, "skipped": True, "reason": f"已是 {status}", **plan}
    if not apply:
        return {"ok": True, "dry_run": True, **plan}

    bak = backup(f"ch{cid}-status")
    changes = [{"table": "channels", "where": {"id": cid}, "field": "status", "before": current, "after": status}]
    wcon = _write_conn()
    try:
        _apply_changes(wcon, changes)
    finally:
        wcon.close()
    rb = _save_rollback(changes, f"ch{cid}-status")
    logs.record("db.set_channel_status", channel=cid, before=current, after=status,
                reason=reason, backup=bak, rollback=rb)
    return {"ok": True, "changed": True, "backup": bak, "rollback_file": rb, **plan}


def strip_disabled_channels(apply: bool = False) -> dict:
    """对"非启用但仍有 abilities"的渠道：清空 channels.models（切断重建源）+ 关闭 abilities。

    为什么不能只置 enabled=0：New API 有 model_update 系统任务 + 每 60 分钟自动测渠道，
    会按 channels.models 重建 abilities（实测：置 0 后 2 分钟内被刷回 1）。
    """
    path = db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    try:
        chans = con.execute(
            "select c.id, c.name, c.status, length(coalesce(c.models,'')) from channels c "
            "where c.status<>1 and exists(select 1 from abilities a where a.channel_id=c.id and a.enabled=1) "
            "order by c.id").fetchall()
        ability_rows = con.execute(
            "select a.`group`, a.model, a.channel_id from abilities a join channels c on c.id=a.channel_id "
            "where a.enabled=1 and c.status<>1").fetchall()
        models_before = {cid: con.execute("select coalesce(models,'') from channels where id=?", (cid,)).fetchone()[0]
                         for cid, *_ in chans}
    finally:
        con.close()

    plan = {"action": "db.strip_disabled", "channels": [{"id": c, "name": n, "status": st, "models_len": ml}
                                                        for c, n, st, ml in chans],
            "abilities": len(ability_rows)}
    if not apply:
        return {"ok": True, "dry_run": True, **plan}
    if not chans:
        return {"ok": True, "skipped": True, "reason": "没有需要处理的渠道", **plan}

    bak = backup("strip-disabled")
    changes = [{"table": "channels", "where": {"id": cid}, "field": "models",
                "before": models_before[cid], "after": ""} for cid, *_ in chans]
    changes += [{"table": "abilities",
                 "where": {"group": g, "model": m, "channel_id": cid},
                 "field": "enabled", "before": 1, "after": 0} for g, m, cid in ability_rows]
    wcon = _write_conn()
    try:
        _apply_changes(wcon, changes)
    finally:
        wcon.close()
    rb = _save_rollback(changes, "strip-disabled")
    logs.record("db.strip_disabled", channels=[c for c, *_ in chans], abilities=len(ability_rows),
                backup=bak, rollback=rb)
    return {"ok": True, "channels_stripped": len(chans), "abilities_disabled": len(ability_rows),
            "backup": bak, "rollback_file": rb, **plan}


def rollback(rollback_file: str, apply: bool = False) -> dict:
    """按回滚文件把字段还原。"""
    data = atomic.read_json(rollback_file, default=None)
    if not data:
        return {"ok": False, "error": f"回滚文件不存在或不可读：{rollback_file}"}
    changes = data.get("changes") or []
    if not apply:
        return {"ok": True, "dry_run": True, "would_restore": len(changes),
                "tables": sorted({c["table"] for c in changes})}
    restore = [{"table": c["table"], "where": c["where"], "field": c["field"],
                "before": c["after"], "after": c["before"]} for c in changes]
    backup("rollback")
    wcon = _write_conn()
    try:
        _apply_changes(wcon, restore)
    finally:
        wcon.close()
    logs.record("db.rollback", file=rollback_file, restored=len(restore))
    return {"ok": True, "restored": len(restore)}


def _connect(path: str, timeout: float = 8.0) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)


def summary() -> dict:
    path = db_path()
    try:
        con = _connect(path)
    except sqlite3.Error as exc:
        return {"ok": False, "path": path, "error": f"打不开（可能被写锁）：{exc}"}
    try:
        cur = con.cursor()
        out: dict[str, Any] = {"ok": True, "path": path}
        out["channels"] = cur.execute("select count(*) from channels").fetchone()[0]
        out["channels_enabled"] = cur.execute("select count(*) from channels where status=1").fetchone()[0]
        out["abilities"] = cur.execute("select count(*) from abilities").fetchone()[0]
        out["abilities_enabled"] = cur.execute("select count(*) from abilities where enabled=1").fetchone()[0]
        out["groups"] = [r[0] for r in cur.execute("select distinct `group` from abilities order by 1 limit 30")]
        out["models"] = [r[0] for r in cur.execute("select distinct model from abilities order by 1 limit 30")]
        return out
    except sqlite3.Error as exc:
        return {"ok": False, "path": path, "error": str(exc)}
    finally:
        con.close()


def _status_label(status: int) -> str:
    return {1: "启用", 2: "手动停用", 3: "自动停用", 0: "异常状态(0)"}.get(status, f"未知({status})")


def audit() -> dict:
    """返回 {ok, findings:[{level,code,detail}], stats:{...}}。只读。"""
    path = db_path()
    findings: list[dict] = []
    try:
        con = _connect(path)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"打不开：{exc}", "findings": [], "stats": {}}
    try:
        cur = con.cursor()

        # A. group 含逗号 —— 永远路由不到
        bad_groups = cur.execute(
            "select `group`, count(*) from abilities where `group` like '%,%' group by `group` limit 20"
        ).fetchall()
        if bad_groups:
            findings.append({"level": "error", "code": "A-comma-group",
                             "detail": "abilities.group 含逗号（永远路由不到）：" +
                                       "；".join(f"{g}×{n}" for g, n in bad_groups[:6])})

        # B. abilities 指向不存在/已停用的渠道（按影响行数排序）
        orphan = cur.execute(
            "select a.channel_id, c.status, count(*) n from abilities a "
            "left join channels c on c.id=a.channel_id "
            "where a.enabled=1 and (c.id is null or c.status<>1) group by a.channel_id order by n desc limit 20"
        ).fetchall()
        if orphan:
            total = sum(n for _, _, n in orphan)
            head = "；".join(
                f"ch{cid}({_status_label(st) if st is not None else '不存在'})×{n}" for cid, st, n in orphan[:4])
            findings.append({"level": "error", "code": "B-orphan-abilities",
                             "detail": f"abilities 指向非启用/不存在渠道：{head}"
                                       f"{'…' if len(orphan) > 4 else ''}（合计 {total} 行）"})

        # C. 同一 (group, model, channel) 重复行
        dup = cur.execute(
            "select `group`, model, channel_id, count(*) n from abilities "
            "group by `group`, model, channel_id having n>1 limit 10"
        ).fetchall()
        if dup:
            findings.append({"level": "warn", "code": "C-duplicate-abilities",
                             "detail": "重复 abilities 行：" +
                                       "；".join(f"{g}/{m}/ch{cid}×{n}" for g, m, cid, n in dup[:5])})

        # D. 渠道非启用但 abilities 仍启用（残留路由）——注意 status=0 是异常值
        residue = cur.execute(
            "select c.id, c.name, c.status, count(*) n from channels c "
            "join abilities a on a.channel_id=c.id and a.enabled=1 "
            "where c.status<>1 group by c.id order by n desc limit 10"
        ).fetchall()
        if residue:
            total = sum(n for *_, n in residue)
            head = "；".join(f"ch{cid}({name},{_status_label(st)})×{n}" for cid, name, st, n in residue[:4])
            anomaly = [f"ch{cid}" for cid, _, st, _ in residue if st == 0]
            findings.append({"level": "error", "code": "D-residual-routing",
                             "detail": f"渠道非启用但仍挂启用中的 abilities：{head}"
                                       f"{'…' if len(residue) > 4 else ''}（合计 {total} 行）"
                                       + (f"；其中 {','.join(anomaly)} 是 status=0 异常值，需人工核对" if anomaly else "")})

        # E. Key 为空 / 掩码 / 过短
        badkey = cur.execute(
            "select id, name, length(key) from channels "
            "where status=1 and (key is null or length(trim(key))<10 or key like '%*%') limit 10"
        ).fetchall()
        if badkey:
            findings.append({"level": "warn", "code": "E-bad-key",
                             "detail": "启用渠道的 Key 可疑（空/掩码/过短）：" +
                                       "；".join(f"ch{cid}({name},len={ln})" for cid, name, ln in badkey[:5])})

        # F. 单点：某个 (group, model) 只有 1 个可用渠道
        single = cur.execute(
            "select `group`, model, count(distinct channel_id) n from abilities where enabled=1 "
            "group by `group`, model having n=1 order by model limit 40"
        ).fetchall()
        focus = [r for r in single if r[1] in ("deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4-flash")]
        if focus:
            findings.append({"level": "info", "code": "F-single-point",
                             "detail": "单点路由（只有 1 个渠道供货）：" +
                                       "；".join(f"{g}/{m}" for g, m, _ in focus[:6])})

        # G. 路由矩阵：某 (group, model) 的 priority 分布（用于人工核对“谁在前面”）
        matrix = cur.execute(
            "select `group`, model, channel_id, priority from abilities where enabled=1 "
            "and model like '%deepseek%' order by `group`, model, priority desc limit 60"
        ).fetchall()
        stats = {"single_point_total": len(single)}
        return {"ok": True, "path": path, "findings": findings, "stats": stats,
                "matrix_sample": [{"group": g, "model": m, "channel": c, "priority": p} for g, m, c, p in matrix]}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"查询失败：{exc}", "findings": findings, "stats": {}}
    finally:
        con.close()
