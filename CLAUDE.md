# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此代码库中工作时提供指导。

## 项目概述

Harness-Lite 是一个轻量级大模型多智能体编排框架，支持异步工具调用、对话记忆、安全沙箱和基于技能的 SOP 规范。

## 常用命令

```bash
# 安装包
pip install -e .

# 运行 CLI
harness-lite "任务"                     # 单轮对话
harness-lite -i                        # 交互模式（自动生成会话）
harness-lite -i --session <id>         # 继续指定会话

# 运行测试
pytest tests/ -v
```

## 架构

### 核心模块依赖链

```
Config → Memory → Registry → Security → Loop (Engine + Strategy) → CLI
                              ↓
                          Tools/Skills
```

### 执行流程 (ReAct 循环)

```
用户输入 → AsyncLoopEngine → ReActStrategy.execute()
    → call_llm_async() → [tool_calls?] → process_tool_calls_async()
    → _execute_tool() → security.intercept() → tool.execute()
    → memory.save_context() → 返回响应
```

### 核心组件

| 模块 | 路径 | 职责 |
|------|------|------|
| **Config** | `config/loader.py` | 从 `.env` 加载 LLM 配置 |
| **Memory** | `memory/store.py`, `memory/manager.py` | JSON 文件存储，位于 `memory_store/{日期}/{会话ID}.json` |
| **Registry** | `registry/base.py` | `Tool` 和 `Skill` 抽象基类 |
| **ToolRegistry** | `registry/tool_registry.py` | 插件注册表，提供 `register()`、`get()`、`get_all_schemas()` |
| **SkillRegistry** | `registry/skill_registry.py` | 技能注册表，提供 `register()`、`list_all()` |
| **Security** | `security/manager.py` | `SecurityManager` - 沙箱隔离、路径重写、危险命令拦截 |
| **AsyncLoopEngine** | `loop/engine.py` | 异步 LLM 编排器，基于 httpx，含指数退避重试 |
| **ReActStrategy** | `loop/strategy.py` | ReAct 循环策略，支持上下文裁剪和熔断机制 |
| **CLI** | `cli/app.py` | Typer 应用，提供流式输出和状态回调的 `CLIOutputHandler` |

### 12 个内置工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `calculator` | `tools/calculator.py` | 基于 AST 的安全数学计算 |
| `current_time` | `tools/current_time.py` | 格式化日期时间 |
| `list_directory` | `tools/file_ops.py` | 目录树形结构（带深度限制） |
| `read_file` | `tools/file_ops.py` | 按行号范围读取文件 |
| `create_file` | `tools/file_ops.py` | 安全创建文件（不覆盖已有文件） |
| `edit_file` | `tools/file_ops.py` | 基于行号区间的替换 |
| `grep_search` | `tools/file_ops.py` | 递归内容搜索 |
| `bash_terminal` | `tools/execution_ops.py` | 持久化 Shell（带状态记忆） |
| `python_interpreter` | `tools/execution_ops.py` | 临时文件 Python 执行 |
| `intelligence_search` | `tools/web_ops.py` | Tavily API 网络搜索 |
| `web_scraper` | `tools/web_ops.py` | BeautifulSoup HTML 抓取 |
| `read_skill` | `tools/skill_reader.py` | 读取 SKILL.md 内容 |

### 技能系统

技能是以 SOP 手册形式存储在 `src/harness_lite/skills/{技能名}/SKILL.md` 中的文档。导入时自动加载。解析 YAML frontmatter 获取 `name` 和 `description`。

## 重要实现说明

### 工具 Schema 格式
所有工具必须使用嵌套的 OpenAI function-calling 格式：
```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "...",
        "parameters": {"type": "object", "properties": {...}}
    }
}
```

### 安全沙箱
- 工作区锁定在 `sandbox/` 目录（可通过 `WORKSPACE_ROOT` 环境变量配置）
- 文件工具的路径会被重写为沙箱内的绝对路径
- 危险 Shell 命令通过正则表达式拦截（如 `rm -rf`、`curl|wget |bash` 等）

### 流式输出
- 流式输出使用 `sys.stdout.write()` 而非 `print()`
- `CLIOutputHandler` 提供逐字符打字机效果和状态回调
- 状态回调展示模型思考过程和工具执行进度

### 异步模式
- `AsyncLoopEngine.run()` 是异步的，但 CLI 用 `asyncio.run()` 包装
- 策略模式：`ReActStrategy.execute()` 委托引擎执行 LLM 调用
- 工具是同步的，但用 `asyncio.to_thread()` 包装以避免阻塞

### 会话管理
- 会话存储在 `memory_store/{YYYY-MM-DD}/{session-id}.json`
- CLI 不带 `--session` 时创建 `session-{时间戳}`
- 继续会话时自动加载历史记忆

### LLM 集成
- 默认使用 MiniMax API（通过 `.env` 配置）
- 通过 `httpx.AsyncClient` 调用 `{base_url}/chat/completions`
- 支持 SSE 流式响应
- 网络错误时 3 倍指数退避重试

## 常见错误模式（见 .claude/ERRORS.md）

- Typer `Option()` 返回 `OptionInfo` - 需要手动解析 `sys.argv` 获取实际值
- MiniMax 工具格式要求嵌套的 `{"type": "function", "function": {...}}` schema
- 记忆路径使用日期分级目录：`memory_store/{日期}/{会话ID}.json`
- 流式输出必须用 `sys.stdout.write()` + flush，禁止用 `print()`
- 工具调用中间响应不应展示给用户
