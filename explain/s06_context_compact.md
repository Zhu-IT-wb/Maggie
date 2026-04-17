# s06_context_compact

## 这一节做了什么

`s06_context_compact.py` 在 `s05_skill_loading.py` 的基础上增加了两类能力：

1. 上下文压缩
2. 会话持久化与恢复

这一层的目标不只是“让对话变短”，而是让 Maggie 在长会话中还能继续工作，并且在程序退出后还能恢复之前的上下文。

## 核心组成

### 1. 三层压缩机制

位置：[maggie/compression.py](/F:/agent_work_space/Maggie/maggie/compression.py:1)

`s06` 里的压缩不是单一动作，而是三层机制：

#### 第一层：`micro_compact()`

这是每轮调用前都会执行的静默压缩。

它会：

- 找出较早的 `tool_result`
- 保留最近几次工具结果
- 对更早的大结果做占位替换

例如可能把旧结果替换成：

```text
[Previous: used bash]
```

这样做的作用是：

- 保留执行轨迹
- 减少长输出对上下文的占用
- 避免模型反复带着很大的旧结果继续推理

当前默认会优先保留：

- `read_file`
- `load_skill`

因为这两类结果通常属于参考资料，过早压缩会影响后续推理。

#### 第二层：`auto_compact()`

当上下文估算超过阈值时，自动触发压缩。

当前阈值定义在：

```python
TOKEN_THRESHOLD = 50000
```

自动压缩会做三件事：

1. 保存压缩前的完整历史
2. 调模型生成连续性摘要
3. 用摘要替换当前 `messages`

这样后续会话还能继续，但不需要一直带着完整旧历史。

#### 第三层：`compact` 工具

父 agent 可以主动调用 `compact`。

这是手动压缩入口，适合下面这种情况：

- 对话还没超阈值
- 但模型自己认为上下文已经太乱
- 希望主动整理一次

它还支持一个 `focus` 字段，用来告诉压缩器优先保留什么。

## 2. 会话持久化

位置：[maggie/session_store.py](/F:/agent_work_space/Maggie/maggie/session_store.py:1)

这一版 `s06` 最大的增强点，是把会话状态从“纯内存”变成了“磁盘可恢复”。

每个会话都会有一个 `session_id`，并存放在：

```text
.sessions/<session_id>/
```

其中包括：

- `state.json`
  - 当前消息历史
  - transcript 索引
  - 创建时间
  - 更新时间
- `transcripts/`
  - 每次压缩前保存的完整转录
- `exports/`
  - 手动导出的 Markdown 会话副本
  
此外还有：

```text
.sessions/index.json
```

用于记录当前最新会话。

## 3. transcript 的作用

`s06` 里的 transcript 已经不是单纯的调试垃圾文件了。

每次触发压缩时，会保存一份压缩前的完整历史到：

```text
.sessions/<session_id>/transcripts/transcript_xxx.jsonl
```

然后在 `state.json` 里记录这份 transcript：

- 文件路径
- 创建时间
- focus
- 摘要预览

这意味着 transcript 现在属于某个具体 session，并且可以被追溯。

## 4. 会话恢复

位置：[agents/s06_context_compact.py](/F:/agent_work_space/Maggie/agents/s06_context_compact.py:1)

`s06` 现在支持三种恢复方式：

### 启动时恢复最近会话

```powershell
python agents\s06_context_compact.py --resume latest
```

### 交互中恢复最近一个非当前会话

```text
/resume latest
```

这个命令会跳过当前会话，恢复最近一个历史会话，避免“刚创建的新空会话覆盖 latest”的问题。

### 按 session_id 精确恢复

```text
/resume abc12345
```

这样可以直接恢复指定会话。

## 5. 会话查看与导出

### 查看当前会话

```text
/session
```

### 查看所有会话

```text
/sessions
```

会列出：

- `session_id`
- `message_count`
- `transcript_count`
- `updated_at`

### 导出当前会话

```text
/session export
```

导出后会生成一个 Markdown 文件，位置类似：

```text
.sessions/<session_id>/exports/session_<session_id>.md
```

导出内容包括：

- 会话基本信息
- 当前消息历史
- transcript 列表

这适合你做复盘、归档或整理成笔记。

## 6. 会话清理

为了防止 `.sessions/` 一直膨胀，`s06` 增加了清理能力。

### 默认清理

```text
/cleanup
```

默认只保留最近 1 个会话。

### 指定保留数量

```text
/cleanup 3
```

表示保留最近 3 个会话，其余删除。

## 7. 主循环里发生了什么

`s06` 的主循环大致流程是：

1. 先执行 `micro_compact()`
2. 如果 token 超阈值，执行 `auto_compact()`
3. 调用模型
4. 执行工具
5. 把结果回写进消息历史
6. 把当前消息快照保存到 `state.json`
7. 如果模型调用了 `compact`，执行手动压缩

所以它已经不是一个纯“聊天循环”，而是一个：

- 能做工具调用
- 能跟踪待办
- 能加载技能
- 能委派子 agent
- 能压缩上下文
- 能持久化并恢复会话

的增强版父 agent。

## 为什么这样设计

`learn-claude-code` 原始 `s06` 更偏教学：

- 演示压缩会发生
- 演示 transcript 会落盘

但如果想自用，只做到这一步是不够的，因为：

- transcript 会越积越多
- 没有恢复能力
- 文件只是日志，不是资产

Maggie 这一版把它推进成“可自用会话层”的原因就是：

- 压缩不该只是丢历史
- 历史应该能恢复
- transcript 应该属于 session
- session 应该可以查看、导出、清理

## 和 s05 的关系

`s05` 解决的是：

- 父 agent 如何按需加载技能知识

`s06` 解决的是：

- 父 agent 在长对话下如何继续工作
- 如何把长对话变成可恢复的会话资产

所以 `s06` 可以理解为：

```text
s06 = s05 + context compaction + session persistence + resume/export/cleanup
```