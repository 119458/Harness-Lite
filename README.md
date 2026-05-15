# Harness-Lite

轻量级大模型多智能体编排框架，支持工具调用、对话记忆与安全拦截。

## 项目起源

Harness-Lite 借鉴了 **OpenHarness** 项目（遵循 [MIT License](https://opensource.org/licenses/MIT)）。感谢 OpenHarness 团队在 LLM Agent 编排领域所做的开创性工作。

## 功能特性

- **工具调用**：内置工具（计算器、获取当前时间、搜索），支持扩展插件注册
- **对话记忆**：基于 JSON 的会话存储，按日期/会话 ID 分类持久化
- **安全拦截**：权限校验、输入验证与审计日志
- **LLM 编排**：流式响应、工具调用循环与对话历史管理

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