#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提交前密钥扫描：在 `git add` 之前拦住明文密钥/账号。

用法：
    python tools/precommit_secret_scan.py            # 扫描工作区（含未跟踪文件）
    python tools/precommit_secret_scan.py --staged   # 只扫 git 暂存区（需要 git）

退出码：0=干净，1=发现疑似密钥（应当阻止提交）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}")),
    ("qq_authcode", re.compile(r"^\s*code\s*=\s*[a-z]{16}\s*$", re.M)),
    ("password_field", re.compile(r'"(password|passwd|token|api_?key)"\s*:\s*"[^"]{12,}"')),
    ("newapi_key", re.compile(r"sk-[A-Za-z0-9]{40,}")),
]
SKIP_DIRS = {".git", "__pycache__", "state", "logs", "node_modules", ".venv"}
SKIP_SUFFIX = {".log", ".jsonl", ".pyc", ".db", ".zip"}
ALLOW_FILES = {"secrets.example.json", "precommit_secret_scan.py"}


def iter_files(staged: bool):
    if staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                             cwd=ROOT, capture_output=True, text=True)
        for name in (out.stdout or "").splitlines():
            p = ROOT / name.strip()
            if p.is_file():
                yield p
        return
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in SKIP_SUFFIX or fn in ALLOW_FILES:
                continue
            yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args()

    hits = []
    for p in iter_files(args.staged):
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            continue
        for name, rx in PATTERNS:
            for m in rx.finditer(text):
                frag = m.group(0)
                hits.append({"file": str(p.relative_to(ROOT)), "rule": name,
                             "sample": frag[:14] + "…"})
                break

    if not hits:
        print("✓ 未发现疑似密钥")
        return 0
    print(f"✗ 发现 {len(hits)} 处疑似密钥（应阻止提交）：")
    for h in hits:
        print(f"  {h['file']:<48}{h['rule']:<16}{h['sample']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
