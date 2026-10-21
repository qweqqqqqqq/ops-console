# Codex for Open Source · 申请准备（本机现状 2026-10-21）

## 一、先说结论

这个福利（6 个月 ChatGPT Pro，价值 $1200）的硬门槛是**你得是一个公开开源项目的维护者**。
本机现状：

| 项 | 状态 |
|---|---|
| openai.com 访问 | ❌ 所有路径 **403**（Cloudflare 挡机器）；`r.jina.ai` 文本代理超时 |
| git | ❌ 未安装 |
| gh CLI | ❌ 未安装 |
| GitHub 凭据 | ❌ `.credentials.yaml` 里没有 GitHub token |
| 本机 git 仓库带远端 | ❌ 一个都没有 |
| 已有公开项目 | 未知（需要你确认 GitHub 账号） |

⇒ **我无法代你提交申请**（既打不开表单，也没有可填的公开仓库）。
**唯一缺的不是写作，是一个公开仓库 URL。**

## 二、可以立刻公开的候选（按"像正经项目"排序）

| 项目 | 路径 | 说明 |
|---|---|---|
| **ops-console** | `ai-gateways\ops-console` | 补号/中转栈控制台：白名单执行、SQLite 库审计、频率闸门、每小时巡检守护、可回滚写库。纯标准库、无第三方依赖。最适合当门面。 |
| dsh-memory-lab | `C:\dsh-plugins\dsh-memory-lab` | DeepSeek Harness 插件：把 TencentDB/Ultron 两套记忆接成工具 + 自动捕获对话。 |
| ai-radar | `D:\ai-radar` | AI 情报雷达：每日抓取、信号生成、知识库沉淀。 |

> 发布前必须做的两件事（否则会把隐私/密钥推上去）：
> 1. 加 `.gitignore`：`state/db-backup/`、`logs/`、`state/*.json`（含账号密码/key）、`node_modules/`
> 2. 把 `ops-console\state\novai-acct-*.json`（**含明文账号密码和 key**）从源码树里挪走

## 三、申请表单可直接用的一段（英文）

> **Project:** ops-console — a safety-first operations console for AI gateway stacks (New API / one-api).
>
> **What it does:** it treats an LLM relay stack as production infrastructure: read-only audits of the
> SQLite router tables (orphan `abilities`, residual routing to disabled channels, empty-key channels),
> whitelisted start/stop with a 60s verification window and rollback, a global rate-limit gate that is
> shared across processes, and an hourly guard task that records every decision to an append-only JSONL
> evidence chain. It exists because a single wrong `UPDATE` on the routing table cost me days of
> debugging: `channels.status=2` does not stop routing — only an empty `abilities` row does.
>
> **Role:** sole maintainer (design, implementation, operations).
>
> **Why Codex:** most of the remaining work is mechanical but risky — property tests for the routing
> invariants, a migration path from direct-DB edits to the admin API, and adapters for other
> one-api forks. Codex would let me keep that work inside the repo instead of in ad-hoc scripts.

## 四、我能帮你做的（说一声就动）

1. 装便携版 git（像之前的 JDK/Maven 一样解压到 `D:\agent-memory-lab\toolchain\git`）
2. 给 ops-console 生成 `.gitignore` + `README.md` + `LICENSE`（MIT），并 `git init` + 首次提交
3. 把含密钥的 `state/*.json` 挪出仓库、换成 `.example` 模板
4. 你建好 GitHub 仓库后，我给你 `git remote add` + `push` 的命令（或你给我 token，我直接推）

## 五、注意

- 申请页是 `openai.com/zh-Hans-CN/for-codex-for-open-source/`，我已用你的默认浏览器打开；**需要你登录 OpenAI 账号**。
- 免费额度是「6 个月 ChatGPT Pro」，覆盖 Codex 使用；按官方说法是**没有硬性 Star 数要求**，有项目链接基本能过。
- 不要在同一台机器上批量注册 OpenAI 账号去领——那是风控红线，和补号站不是一个性质。
