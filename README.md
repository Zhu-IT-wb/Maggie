# Maggie

Maggie 是一个面向本地开发场景的编程 Agent 运行时，定位是“能执行、可协作、可恢复”的工程助手，而不是只会对话的 Demo。

当前对外统一入口：`agents/main.py`。

## 项目定位

Maggie 重点解决的是多步骤编码任务在真实工作区中的稳定执行问题：

- 任务状态不依赖聊天上下文
- 长会话可恢复、可压缩、可导出
- 工具调用与执行状态可追踪
- 多 Agent 协作可协议化管理
- 长耗时命令可异步脱离主对话执行

## 核心能力

- 持久化任务板：`task_create / task_update / task_list / task_get`
- 会话管理：`/resume`、`/session export`、`/cleanup`
- 工具执行：`shell / read_file / write_file / edit_file`
- 子任务隔离：`task`（一次性 subagent）
- 团队协作：`spawn_teammate / send_message / read_inbox / broadcast`
- 协议能力：`plan_approval`、`shutdown_request / shutdown_response`
- 自治队友：`idle`、`claim_task`
- 后台任务：`background_run / check_background`
- 技能加载：`load_skill`

## 架构设计

Maggie 采用“两平面”模型：

- 控制面（Control Plane）：任务、会话、协议、队友状态
- 执行面（Execution Plane）：工具执行、后台任务、子代理

推荐阅读完整架构文档：[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)

## 快速开始

1. 复制 `.env.example` 为 `.env`
2. 填写模型与 API Key（支持 OpenAI 兼容与 Anthropic 路线）
3. 启动 Maggie：

```powershell
python agents\main.py
```

## 常用命令

- `/session`：查看当前会话 ID
- `/sessions`：列出可恢复会话
- `/resume latest`：恢复最近会话
- `/resume <session_id>`：恢复指定会话
- `/cleanup <N>`：仅保留最近 N 个会话
- `/session export`：导出当前会话为 Markdown
- `/team`：查看队友状态
- `/inbox`：查看 lead inbox

## 推荐使用方式

1. 先建立长期任务板，再开始执行：任务用于协调，工具用于执行。
2. 长任务优先使用会话恢复与压缩：保证上下文可持续。
3. 长耗时命令优先放到后台：避免阻塞主对话循环。
4. 多 Agent 协作尽量走协议化流程：减少“各做各的”漂移。

## 目录结构

- `agents/main.py`：统一 CLI 入口
- `agents/s11_autonomous_agents.py`：当前最终运行时核心
- `maggie/`：核心模块（llm、tools、tasks、team、autonomy、session）
- `skills/`：按需加载的技能文档
- `docs/`：架构与说明文档

## 当前边界

- 部分流程仍依赖模型遵守协议（非全硬性约束）
- 安全策略是工程级防护，不等同于操作系统级沙箱
- 设计目标是本地单操作者工作流，不是分布式调度系统
