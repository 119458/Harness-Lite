# Harness-Lite

轻量级大模型多智能体编排框架，支持工具调用、对话记忆与安全拦截。

## 项目起源

Harness-Lite 借鉴了 **OpenHarness** 项目（遵循 [MIT License](https://opensource.org/licenses/MIT)）。感谢 OpenHarness 团队在 LLM Agent 编排领域所做的开创性工作。

## 功能特性

- **工具调用**：内置工具（计算器、获取当前时间、搜索），支持扩展插件注册
- **对话记忆**：基于 JSON 的会话存储，按日期/会话 ID 分类持久化
- **安全拦截**：权限校验、输入验证与审计日志
- **LLM 编排**：流式响应、工具调用循环与对话历史管理

## 🚀 [2026-05-18] 循环编排功能优化
- **异步并发调度 (Async Parallel Dispatch)**：基于 `asyncio` 与大模型 Parallel Tool Calling 能力，支持在同一回合内并发执行多个独立工具，极大降低网络 I/O 耗时。
- **自省容错与熔断 (Error Recovery & Circuit Breaker)**：内置大模型 JSON 格式纠错重试、工具执行异常隔离与反馈引擎，以及达到最大推理步数后的系统级熔断保护。
- **沉浸式终端交互 (Immersive CLI UI)**：终端内置滚动状态指示器（平滑展示大模型思考与工具并行的中间态日志），并配备打字机式丝滑流式输出。
- **策略模式编排 (Strategy Orchestration)**：解耦底层引擎与编排逻辑，内置高度容错的 `ReActStrategy`，可轻松扩展至图驱动 (Graph) 或 Plan-and-Execute 等复杂流转模式。

## 🚀 [2026-05-19] 构建完备的tool与skill技能
- 添加常规了文件创建、查找、修改等功能
- 增加了可运行终端的tool(目前还比较初级)
- 增加了搜索tool
- 增加了skill技能工具
- 详细信息查看function_description中的tool-skill-2026-5-19.md

## 🚀 [2026-05-26] 构建security和memory
- 建立3级审查安全，当不通过时交由人工判断
- 建立短期记忆和长期记忆（md文件）
- 上下文智能压缩，先压缩工具调用 -> 早期记忆(默认上下午64k)
- 模型调用由之前的httpx换位openai
- 将模型输出有误的content进行处理，确保其不影响下次的模型对话使用
- 测试
  生成一个贪吃蛇程序并运行，其会在规定的沙箱环境中生成一份py代码并在创建的子进程上调用程序
  可以成功调用crate，edit，bash等工具。

## 🚀 [2026-05-27] cli优化和添加模型思考开关
- 将之前通过input的用户输出改为prompt_toolkit库来处理，异步处理用户输入，使得终端的用户输入连续不卡顿
- 在.env中添加LLM_THINKING_MODE=false/true的关键字，可以开启大模型思考获取关闭大模型思考

## 架构

```
Config → Memory → Registry → Security → Loop → CLI
                         ↓
                      Tools/Skills
```

| 模块 | 路径 | 职责 |
|------|------|------|
| Config | `src/harness_lite/config/` | 从 `.env` 加载 LLM 配置 |
| Memory | `src/harness_lite/memory/` | JSON 会话存储 |
| Registry | `src/harness_lite/registry/` | 工具/技能插件注册 |
| Loop | `src/harness_lite/loop/engine.py` | 核心 LLM 编排引擎 |
| CLI | `src/harness_lite/cli/app.py` | Typer 命令行入口 |

## 安装

```bash
pip install -e .
```

## 使用

```bash
# 交互模式（自动生成会话）
harness-lite -i

# 继续指定会话
harness-lite -i --session <session-id>

# 单次任务（自动生成会话）
harness-lite "你的任务"
```

## 测试

```bash
pytest tests/ -v
```

## 许可证

MIT License — 原始项目见 [OpenHarness](https://github.com/example/open-harness)。