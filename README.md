# ops-console — 面向 AI 中转栈的操作控制台

> 把「一个 LLM 中转站」当生产系统来管：只读审计 → 白名单执行 → 可回滚 → 全程留证。
> 针对 [New API / one-api](https://github.com/QuantumNous/new-api) 系（SQLite 单库、`channels` + `abilities` 路由表）。

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ L0 观测      │ → │ L1 诊断       │ → │ L2 执行       │ → │ L3 记忆       │
│ 服务/端口/任务 │   │ 硬规矩体检     │   │ 白名单+回滚    │   │ 决策 JSONL    │
└─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

## 为什么会有这个项目

一条实测出来的结论：

> **`channels.status = 2` 挡不住路由。** 只要 `abilities` 里还有一行 `enabled = 1`，
> 请求就会被打到这个"已停用"的渠道上——返回 `HTTP 200`，正文却是上游的"余额不足"。
> 唯一的止血办法是让 `abilities` 没有行。

这条坑是拿真实请求验证出来的（见 `docs/DESIGN.md` §8.5）：一个只有该渠道声明的模型，
停用后依然 200。而当时我以为是"改库不生效"，实际是三层原因叠加：

1. 运行中的 New API 会把内存里的渠道缓存**周期性写回 SQLite**，外部改库两分钟内被覆盖；
2. 只置 `enabled=0` 会被下一次 `model_update` 重建刷回 `1`；
3. 重建的来源是 `channels.models`，所以**必须连 models 一起清**，再重启服务加载干净缓存。

## 快速开始

```bash
# 0) 只依赖 Python 3.9+ 标准库，无需 pip install
python manage.py status        # 全景：服务 / 端口 / 进程 / 计划任务
python manage.py doctor        # 7 项硬规矩体检（含 New API 库一致性）
python manage.py db            # 库审计：孤儿 abilities / 残留路由 / 空 Key / 路由矩阵
python manage.py stop cf_adapter          # 默认 dry-run，只打印计划
OPS_CONSOLE_CONFIRM=service.stop \
python manage.py stop cf_adapter --apply  # 真执行（需确认令牌）
python manage.py pipeline plan # 补号流水线下一步 + 配额 + 频率闸门
```

## 设计要点

| 层 | 文件 | 关键约束 |
|---|---|---|
| 观测 | `console/state.py` | 只读；把 config/services.json、服务管理器、端口、`schtasks` 合并成一份快照 |
| 诊断 | `console/checks.py`、`console/newapi_db.py` | 只读打开 SQLite（`mode=ro`）；把"踩过的坑"变成可执行检查 |
| 执行 | `console/actions.py` | 白名单 + 禁区硬拦 + 单实例锁 + 60s 验证窗口 + 失败回滚 |
| 闸门 | `console/ratelimit.py` | 跨进程限速（邮件 38s、注册 190s…），落盘取号 |
| 编排 | `console/pipeline.py` | 11 阶状态机，破坏性阶段默认拦截 |
| 记忆 | `guard.py` | 每小时巡检；把决策沉淀到外部经验库；默认只报告 |

### 安全模型（都写在代码里，不靠自觉）

- **白名单驱动**：未在 `config/services.json` 登记的服务一律不碰
- **禁区二次校验**：配置写坏了也拦得住（端口 / 进程名 / 危险命令模式）
- **破坏性动作三闸门**：`--apply` + 确认令牌（`OPS_CONSOLE_CONFIRM`）+ 单次动作上限
- **单实例锁**：文件锁 + `tasklist` 判活（Windows 上绝不用 `os.kill(pid, 0)`，那等于杀人）
- **原子写 + 读侧重试**：`tmp+fsync+replace`，读侧也容忍 `PermissionError`
- **子进程无窗口无管道**：`Popen + DEVNULL + CREATE_NO_WINDOW`，避免孙进程继承管道导致死锁
- **可回滚**：每次写库前备份 + 生成 `db-rollback-*.json`，`manage.py db --fix rollback` 一键还原

## 目录

```
manage.py                 唯一入口（status/doctor/db/pipeline/start/stop/...）
guard.py                  每小时巡检（计划任务用 pythonw 调它）
console/                  观测·诊断·执行·闸门·编排
config/                   services / lines / rules / policies（+ secrets.example.json）
tools/                    precommit_secret_scan.py、数据库停服改库脚本等
docs/DESIGN.md            设计文档（含实测踩坑全过程）
```

## 配置

- `config/services.json`：服务清单（端口、能否启停、禁区标记、启动命令）
- `config/policies.json`：阈值、禁区、确认令牌、补号流水线定义
- `config/rules.json`：把 12 条运维硬规矩映射到可执行检查
- `config/secrets.local.json`：本机密钥（**已被 .gitignore 排除**，模板见 `secrets.example.json`）

## 提交前

```bash
python tools/precommit_secret_scan.py --staged   # 命中即拒绝提交
```

## License

MIT
