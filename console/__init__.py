"""补号 / 中转栈控制台 —— 核心包。

设计原则（违反任何一条都算 bug）：
1. 只读优先：status/doctor 永不修改系统状态，只写自己的日志。
2. 白名单驱动：未在 config/services.json 登记的对象一律不动；禁区二次校验。
3. 无窗口：任何子进程都必须 CREATE_NO_WINDOW + 标准流接 DEVNULL。
4. 单实例：写操作前必须拿到文件锁。
5. 原子写 + 双侧重试：JSON/状态文件用 tmp+replace，读写两侧都容忍 Windows PermissionError。
6. 可回溯：每个决策写 JSONL（含 before/after/evidence）。
"""

__version__ = "0.1.0"
