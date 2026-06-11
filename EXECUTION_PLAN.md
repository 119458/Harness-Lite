# Harness-Lite 工具体系重构与扩展方案

## Context（背景与目标）

当前 `src/harness_lite/tools/` 是扁平的单文件结构（7 个 `.py` 文件承载 11 个工具），随着工具数量增加会导致单文件膨胀、职责混杂、难以维护。同时 `adopt-code/tools/` 中调研发现 **browser（浏览器自动化）** 和 **scheduler（定时任务）** 两类能力是当前 Harness-Lite 完全缺失的，加上对 `edit_file` 的模糊匹配增强、`web_scraper` 的多文档解析增强需求，需要系统性重构与扩展。

**本次目标**：
1. **全量重构**：把现有 7 个工具文件全部拆成"每工具一文件夹"的目录结构
2. **新增 4 类工具**：browser（浏览器）、scheduler（定时任务）、fuzzy_edit（模糊编辑）、doc_fetch（文档抓取）
3. **三层安全 + 工具白名单**：所有新工具接入 `security/manager.py`，并增加细粒度白名单（域名/任务上限/资源限制）
4. **全中文工具描述**：所有 description、参数 description 必须用中文，且**严禁照搬** `adopt-code/` 的代码字面
5. **执行流程明确**：写代码 → `harness-coder` 子 Agent；审查 → `harness-code-reviewer` 子 Agent

---

## 一、新工具清单与价值评估

### ✅ 本次复现（4 个）

| 工具 | 价值 | 理由 |
|------|------|------|
| **browser_automation** | High | 当前缺浏览器能力，Playwright 服务化架构清晰可借鉴 |
| **task_scheduler** | High | 当前无定时任务，自包含可独立运行 |
| **fuzzy_edit** | Medium | 现有 `edit_file` 是行号区间替换，补充按文本片段模糊匹配的能力 |
| **doc_fetch** | Medium | 现有 `web_scraper` 仅 HTML，补充 PDF/Word/Excel/PPT 解析 |

### ❌ 本次不复现（理由）

| 工具 | 不复现理由 |
|------|------|
| ls / write | 已有 `list_directory` / `create_file`，纯重复 |
| bash / read | 已有 `bash_terminal` / `read_file`，安全策略已完善 |
| mcp | 接入工作量大，需独立设计 JSON-RPC 协议层，留作下期专项 |
| vision | 取决于是否对接图像 API，本期暂不引入新依赖 |
| memory_get/search | Harness-Lite 已有三层记忆 + mem0，模型不同需独立设计 |
| env_config / evolution_undo / send | 与 Harness-Lite 架构不匹配，无意义 |

---

## 二、目录结构重构方案

### 重构前（现状）
```
src/harness_lite/tools/
├── __init__.py
├── base.py
├── calculator.py
├── current_time.py
├── execution_ops.py       # 含 BashTerminalTool + PythonInterpreterTool
├── file_ops.py            # 含 5 个文件操作工具
├── web_ops.py             # 含 2 个网络工具
└── skill_reader.py
```

### 重构后（目标）
```
src/harness_lite/tools/
├── __init__.py                       # 统一注册入口（保持 register_all_tools）
├── base.py                           # BaseTool 基类（保留原位置）
├── calculator/
│   ├── __init__.py
│   └── calculator.py
├── current_time/
│   ├── __init__.py
│   └── current_time.py
├── list_directory/
│   ├── __init__.py
│   └── list_directory.py
├── read_file/
│   ├── __init__.py
│   └── read_file.py
├── create_file/
│   ├── __init__.py
│   └── create_file.py
├── edit_file/
│   ├── __init__.py
│   └── edit_file.py
├── grep_search/
│   ├── __init__.py
│   └── grep_search.py
├── bash_terminal/
│   ├── __init__.py
│   ├── bash_terminal.py
│   └── process_manager.py            # 从 execution_ops 拆出 SessionProcessManager + IsolatedPersistentShell
├── python_interpreter/
│   ├── __init__.py
│   └── python_interpreter.py
├── intelligence_search/
│   ├── __init__.py
│   └── intelligence_search.py
├── web_scraper/
│   ├── __init__.py
│   └── web_scraper.py
├── read_skill/
│   ├── __init__.py
│   └── read_skill.py
├── browser_automation/               # 【新增】
│   ├── __init__.py
│   ├── browser_tool.py               # BaseTool 子类
│   ├── browser_service.py            # 后台 Playwright 服务
│   └── snapshot.py                   # DOM 快照生成器
├── task_scheduler/                   # 【新增】
│   ├── __init__.py
│   ├── scheduler_tool.py
│   ├── scheduler_service.py          # 后台轮询线程
│   └── task_store.py                 # JSON 持久化
├── fuzzy_edit/                       # 【新增】
│   ├── __init__.py
│   └── fuzzy_edit.py
├── doc_fetch/                        # 【新增】
│   ├── __init__.py
│   └── doc_fetch.py
└── utils/                            # 【新增共享工具】
    ├── __init__.py
    ├── diff_helper.py                # 模糊匹配 + unified diff（fuzzy_edit 用）
    └── output_truncate.py            # 输出截断（head/tail 两种）
```

### 每个工具文件夹的约定
- `__init__.py` 只暴露工具类，例如 `from .calculator import CalculatorTool`
- 工具主文件名与文件夹同名，与工具名一致（snake_case）
- 大型工具拆出辅助模块（process_manager、service、store 等）

---

## 三、新增 4 个工具详细设计

### 工具 1：`browser_automation`（浏览器自动化）

**定位**：基于 Playwright 的浏览器操作工具，支持页面导航、元素交互、内容快照。

**关键实现**：
- `BrowserService` 单例，运行在专用后台线程，通过 `queue.Queue` 接收命令（线程安全）
- 启动模式：`fresh`（每次新建上下文）/ `persistent`（持久化 cookie 到沙箱内 `browser_profile/`）
- 空闲 5 分钟自动关闭浏览器进程，节约资源
- **DOM 快照机制**：注入 JavaScript 遍历可交互元素，生成 `[ref:1] <button>登录</button>` 形式的紧凑文本，让 LLM 后续可用 `ref` 编号定位
- 死浏览器自愈：发现进程异常时自动重启
- 所有内容输出走 `utils/output_truncate.py` 截断（默认 50KB / 2000 行）

**支持的 action（约 8 种）**：
`navigate` / `click` / `fill` / `scroll` / `snapshot` / `wait_for` / `screenshot` / `close`

**输入 Schema**：
```python
{
    "action": {"type": "string", "enum": [...], "description": "浏览器操作类型"},
    "url": {"type": "string", "description": "目标 URL（仅 navigate 需要）"},
    "ref": {"type": "string", "description": "DOM 元素引用编号（点击/填写时使用）"},
    "selector": {"type": "string", "description": "CSS 选择器（无 ref 时备用）"},
    "text": {"type": "string", "description": "填写文本"},
    "timeout": {"type": "integer", "description": "操作超时秒数，默认 30"}
}
```

**安全边界**：
- Layer 1：URL 白名单/黑名单（默认禁止 `file://`、`chrome://`、内网 IP 段如 `10./192.168./127.`）
- Layer 1：`persistent` 模式的 profile 目录必须在沙箱内
- Layer 2：navigate 动作进入 LLM 语义审计（防止访问钓鱼/敏感站点）
- 工具白名单：单 Session 同时只允许 1 个浏览器实例

**依赖**：`pip install playwright` + 首次 `playwright install chromium`

---

### 工具 2：`task_scheduler`（定时任务）

**定位**：自包含的任务调度系统，支持 cron / interval / once 三种触发方式。

**关键实现**：
- `TaskStore`：使用 `memory_store/scheduler/tasks.json` 持久化（与现有记忆体系同目录，复用沙箱保护）
- `SchedulerService`：单例后台线程，每 30 秒轮询到期任务，10 分钟内逾期可追赶
- 任务执行通过回调注入 ReAct 引擎（不直接执行 shell，仅生成 user message 进入下轮 ReAct）
- 三种 action：
  - `create`：创建任务
  - `list`：列出全部任务
  - `delete`：按 ID 删除
  - `pause` / `resume`：暂停/恢复

**输入 Schema**：
```python
{
    "action": {"type": "string", "enum": ["create", "list", "delete", "pause", "resume"]},
    "task_id": {"type": "string", "description": "任务 ID（除 create 外都需要）"},
    "name": {"type": "string", "description": "任务名称"},
    "schedule_type": {"type": "string", "enum": ["cron", "interval", "once"]},
    "schedule_value": {"type": "string", "description": "cron 表达式 / 间隔秒数 / ISO8601 时间点"},
    "prompt": {"type": "string", "description": "触发时投递给 Agent 的指令"}
}
```

**安全边界**：
- Layer 1：单 Session 最多 20 个活动任务（防资源耗尽）
- Layer 1：interval 最小间隔 60 秒（防高频触发）
- Layer 1：cron 表达式语法校验（`croniter` 库）
- 工具白名单：每个 task 启动一次新 ReAct 时复用主沙箱

**依赖**：`pip install croniter`

---

### 工具 3：`fuzzy_edit`（模糊编辑）

**定位**：补充现有 `edit_file`（按行号替换）的不足，按文本片段模糊匹配后替换。

**关键实现**：
- 优先精确匹配；失败后调用 `utils/diff_helper.py` 中的 `fuzzy_find_text()` 做空白归一化匹配
- 自动处理 BOM、CRLF/LF 差异
- 匹配唯一性校验：多个匹配点直接报错（防误改）
- 输出 unified diff 让 LLM 看到改了什么
- `old_text` 为空字符串时视为追加到文件末尾

**输入 Schema**：
```python
{
    "file_path": {"type": "string", "description": "目标文件路径（必须在沙箱内）"},
    "old_text": {"type": "string", "description": "待替换的原文本片段（空串表示追加）"},
    "new_text": {"type": "string", "description": "替换后的新文本"}
}
```

**安全边界**：
- Layer 1：路径沙箱（复用现有 `_check_path_jail`）
- Layer 1：单次替换上限 200KB（防内存溢出）
- 不进入 Layer 2/3

---

### 工具 4：`doc_fetch`（文档抓取）

**定位**：补充现有 `web_scraper` 不足，抓取并解析 PDF / Word / Excel / PPT。

**关键实现**：
- 通过 `requests` 下载文件到沙箱临时目录
- 根据扩展名/MIME 类型路由解析器：
  - `.pdf` → `pypdf`
  - `.docx` → `python-docx`
  - `.xlsx` → `openpyxl`
  - `.pptx` → `python-pptx`
  - 其他 → 报错引导用户改用 `web_scraper`
- 解析完成后**立即删除临时文件**
- 输出走 `output_truncate.truncate_head()` 截断
- 单文件硬上限 50MB

**输入 Schema**：
```python
{
    "url": {"type": "string", "description": "文档 URL（http/https）"},
    "max_pages": {"type": "integer", "description": "最多解析页数，默认 50"}
}
```

**安全边界**：
- Layer 1：URL 协议白名单（仅 http/https）
- Layer 1：禁止内网 IP / `file://` / `localhost`
- Layer 1：下载大小硬限制 50MB
- Layer 1：临时文件路径强制在沙箱内
- 工具白名单：单 Session 同时下载任务 ≤ 3

**依赖**：`pip install pypdf python-docx openpyxl python-pptx`

---

## 四、三层安全 + 工具白名单接入

### 复用的现有安全层
1. **Layer 1（静态规则）** — `security/manager.py:_validate_layer1_static()` 中**新增分支**：
   - `browser_automation`：URL 黑白名单校验、ref 字段长度限制
   - `task_scheduler`：cron 语法校验、interval 下限、任务数上限
   - `fuzzy_edit`：复用 `_check_path_jail()` 校验 `file_path`
   - `doc_fetch`：URL 协议白名单 + 内网拦截
2. **Layer 2（LLM 语义审计）**：扩展触发集合 `["bash_terminal", "python_interpreter", "browser_automation"]`
3. **Layer 3（人工终审）**：browser 的高危动作（navigate 到非常用域名）触发

### 新增白名单机制（`security/whitelist.py`）
```python
TOOL_QUOTA = {
    "browser_automation": {"max_concurrent_per_session": 1},
    "task_scheduler":     {"max_active_tasks_per_session": 20, "min_interval_seconds": 60},
    "doc_fetch":          {"max_concurrent_per_session": 3, "max_file_size_mb": 50},
}

URL_BLOCKLIST = [
    r"^file://",
    r"^chrome://",
    r"https?://(localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)",
]
```

由 `SecurityManager` 在 `intercept()` 中加载并按工具名分发校验。

---

## 五、工具描述/参数中文化规范

### 强制约束
1. 所有 `description` 属性、所有 schema 中 `description` 字段必须用**中文**
2. 函数 docstring 用中文（与 description 保持一致）
3. **严禁照搬** `adopt-code/tools/` 代码：
   - 类名、变量名、方法切分逻辑必须由 `harness-coder` 重新设计
   - 算法思路可借鉴，代码字面不得复制粘贴
   - 注释独立撰写

### 示例（browser_automation 节选）
```python
class BrowserAutomationTool(BaseTool):
    @property
    def name(self) -> str:
        return "browser_automation"

    @property
    def description(self) -> str:
        return ("浏览器自动化工具，基于 Chromium 提供页面导航、元素点击、表单填写、"
                "内容快照等能力。优先通过 snapshot 获取页面元素引用编号，再用 "
                "ref 字段定位操作目标，避免依赖脆弱的 CSS 选择器。")
```

---

## 六、子 Agent 工作流程

### 阶段 A — 重构现有工具（harness-coder）
1. **任务输入**：本计划文件 + 现有 7 个工具文件路径
2. **产出**：每个工具一个文件夹（13 个文件夹），`__init__.py` 改写 import 路径
3. **验收点**：`harness-lite "1+1"` 能正常运行，全部 11 个工具可被调度

### 阶段 B — 新增 4 个工具（harness-coder）
顺序：`utils/` 共享模块 → `fuzzy_edit` → `doc_fetch` → `task_scheduler` → `browser_automation`
（先做轻量的，再做带后台服务的）

### 阶段 C — 安全层扩展（harness-coder）
1. 编辑 `security/manager.py` 新增 4 个工具的 Layer 1 分支
2. 创建 `security/whitelist.py`
3. 扩展 Layer 2 触发集合

### 阶段 D — 测试用例（harness-coder）
在 `tests/` 下新增：
- `test_fuzzy_edit.py`
- `test_doc_fetch.py`
- `test_task_scheduler.py`
- `test_browser_automation.py`（需 `pytest.mark.skipif` 跳过缺 playwright 的环境）
- `test_tool_restructure.py`（验证全部 15 个工具能被 `tool_registry` 注册）

### 阶段 E — 代码审查（harness-code-reviewer）
分两次审查：
1. **第一次**（阶段 A 结束）：审查重构后的目录结构、`__init__.py` 注册逻辑、是否有路径遗漏
2. **第二次**（阶段 B/C/D 结束）：完整审查 4 个新工具 + 安全层扩展，重点关注：
   - 路径沙箱是否被完整覆盖
   - 后台线程的资源清理（browser、scheduler）
   - 工具 Schema 是否符合 OpenAI function-calling 格式
   - 所有描述是否为中文且非抄袭
   - 测试覆盖率

### 阶段 F — 用户验证
按 README 示例跑 5 个场景：
```bash
harness-lite -i  # 进入交互
> 列出所有工具                          # 验证 /tool 看到 15 个
> 用 fuzzy_edit 修改 sandbox/test.py 中的 hello 为 world
> 用 doc_fetch 下载 <PDF链接>
> 用 task_scheduler 创建一个每 5 分钟 ping 一次的任务
> 用 browser_automation 打开 example.com 并截图
```

---

## 七、关键文件清单

### 需要修改的现有文件
| 文件 | 修改内容 |
|------|---------|
| `src/harness_lite/tools/__init__.py` | 重写 import 路径，新增 4 个工具的注册 |
| `src/harness_lite/security/manager.py` | `_validate_layer1_static()` 新增 4 个分支 + 接入白名单 |
| `pyproject.toml` 或 `requirements.txt` | 新增 `playwright`、`croniter`、`pypdf`、`python-docx`、`openpyxl`、`python-pptx` |

### 需要新建的文件（共约 25 个）
- `tools/{calculator,current_time,...,read_skill}/__init__.py` + 同名 `.py`（拆分重构）
- `tools/utils/__init__.py` + `diff_helper.py` + `output_truncate.py`
- `tools/browser_automation/` 下 4 个文件
- `tools/task_scheduler/` 下 4 个文件
- `tools/fuzzy_edit/` 下 2 个文件
- `tools/doc_fetch/` 下 2 个文件
- `security/whitelist.py`
- `tests/` 下 5 个测试文件

### 关键复用代码点
| 复用对象 | 现有位置 |
|---------|---------|
| `BaseTool` 基类 | `src/harness_lite/tools/base.py` |
| 路径沙箱校验 `_check_path_jail` | `src/harness_lite/security/manager.py` |
| Session 上下文 `current_session_id` | `src/harness_lite/tools/execution_ops.py`（重构后挪到 `tools/bash_terminal/process_manager.py`） |
| 工具注册表 `tool_registry` | `src/harness_lite/registry/tool_registry.py` |
| Schema 嵌套格式 `super().get_schema()` | `src/harness_lite/tools/base.py` |
| 记忆管理 `MemoryManager` | `src/harness_lite/memory/manager.py`（task_scheduler 触发记录用） |

---

## 八、验收清单（Verification）

### 自动化验收
```bash
# 1. 安装新依赖
pip install -e .
playwright install chromium

# 2. 跑全量测试
pytest tests/ -v

# 3. 工具数量校验
python -c "from harness_lite.registry.tool_registry import tool_registry; \
           import harness_lite.tools; \
           assert len(tool_registry.list_all()) == 15, '工具数量不符'"

# 4. 安全层校验
pytest tests/test_security.py -v
```

### 人工验收
1. 交互模式跑通"七 - 阶段 F"的 5 个场景
2. 用 `/tool` 命令查看 15 个工具及中文描述
3. 故意触发安全拦截：`browser_automation` 访问 `http://10.0.0.1` 应被 Layer 1 拦截
4. 验证目录结构与本计划"二、目录结构重构方案"完全一致

---

## 九、风险与回滚

| 风险 | 缓解 |
|------|------|
| Playwright 安装失败/平台不兼容 | browser 工具单独 try-import，缺依赖时仅跳过注册不影响其他工具 |
| 重构破坏现有 import 链 | 保持 `from harness_lite.tools import ...` 的对外接口不变；先重构后跑测试 |
| 描述抄袭风险 | harness-code-reviewer 阶段 E 做查重检查 |
| 后台线程泄漏 | 在 `cli/app.py` 退出钩子调用 `shutdown_browser()` 和 `shutdown_scheduler()` |

---

## 十、最终输出

本计划经用户批准后，将**额外写入根目录的 `EXECUTION_PLAN.md`**（与本计划文件内容一致），作为后续执行的入口指引。然后按"六、子 Agent 工作流程"启动阶段 A→F。
