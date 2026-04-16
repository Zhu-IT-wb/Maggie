# s03_todo_write

## 这一节做了什么

`s03_todo_write.py` 在 `s02_tool_use.py` 的基础上增加了一个新的工具：`TodoWrite`。

这一层的目标不是替模型规划任务，而是给模型一个可以持续更新的短任务状态面板。模型可以自己写入、更新、完成待办项，程序只负责校验、保存和回显。

## 核心组成

### 1. TodoManager

位置：[maggie/todo.py](/F:/agent_work_space/Maggie/maggie/todo.py:1)

`TodoManager` 是待办状态的内存管理器，负责三件事：

- `update()`
  - 接收模型通过 `TodoWrite` 提交的 `items`
  - 校验字段是否合法
  - 替换当前待办快照
- `render()`
  - 把当前待办状态渲染成终端可读的清单文本
- `has_open_items()`
  - 判断当前是否还有未完成任务

当前约束有：

- 最多 20 条待办
- `status` 只能是 `pending / in_progress / completed`
- 同时最多只能有 1 条 `in_progress`

## 2. TodoWrite 工具定义

位置：[maggie/tools.py](/F:/agent_work_space/Maggie/maggie/tools.py:1)

`TodoWrite` 被定义为一个标准工具，和 `bash`、`read_file`、`write_file`、`edit_file` 一样，都会作为 schema 提供给模型。

工具输入格式是：

```json
{
  "items": [
    {
      "content": "Inspect repo",
      "status": "completed",
      "activeForm": "Inspecting repo"
    },
    {
      "content": "Add comments",
      "status": "in_progress",
      "activeForm": "Adding comments"
    }
  ]
}
```

这里有两个字段要区分：

- `content`
  - 用于展示任务本身
- `activeForm`
  - 用于在任务处于 `in_progress` 时显示当前动作

例如：

```text
[>] Add comments <- Adding comments
```

## 3. 工具执行流程

位置：[agents/s03_todo_write.py](/F:/agent_work_space/Maggie/agents/s03_todo_write.py:1)

`s03` 的主循环仍然是典型的 tool loop：

1. 把当前 `messages` 和工具列表发给模型
2. 如果模型返回普通文本，直接结束这一轮
3. 如果模型返回 `tool_use`
4. 逐个执行工具
5. 把工具结果作为 `tool_result` 再喂回模型

其中 `TodoWrite` 的执行路径是：

```text
model -> TodoWrite -> execute_tool() -> TodoManager.update() -> render() -> tool_result -> model
```

也就是说，程序不会“理解你的计划”，只是把模型写下来的计划做结构校验并返回当前状态。

## 4. reminder 机制

位置：[agents/s03_todo_write.py](/F:/agent_work_space/Maggie/agents/s03_todo_write.py:1)

这一节还加了一层轻提醒机制：

- 如果模型已经创建了未完成的 todo
- 但连续 3 轮都没有再调用 `TodoWrite`
- 程序就会额外注入一条提醒：

```text
<reminder>Update your todos.</reminder>
```

这个机制的作用不是强制控制模型，而是降低“写了计划但后面忘记更新”的概率。

## 为什么这样设计

`TodoWrite` 的价值不在于规划算法，而在于把模型的短期执行计划变成了一个可见的、结构化的状态：

- 模型自己维护状态
- 程序只负责校验和展示
- 用户可以直接看到当前任务面板
- 后续做 `s04 subagent`、`s07 task system` 时，这种“显式状态”会很容易往更复杂的任务层升级

## 和 s02 的关系

`s02` 解决的是“模型如何调工具”。

`s03` 解决的是“模型在多步骤任务里，如何显式维护自己的短期执行状态”。

所以 `s03` 不是替代 `s02`，而是在 `s02` 的工具循环之上，多加了一个状态写入工具。
