import os
import re
import ast
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Set
from datetime import datetime

# 【核心替换点】引入官方 OpenAI 同步客户端
from openai import OpenAI

from harness_lite.config.loader import get_main_config
from .whitelist import Whitelist, TOOL_QUOTA


# Layer 2 触发集合：工具名 -> 判定是否需要进入 LLM 语义审查的回调
# 仅对真正会产生外部副作用的动作启用 Layer 2，避免高频 action 浪费 token。
LAYER2_TARGETS = {
    "bash_terminal": lambda args: True,
    "python_interpreter": lambda args: True,
    "browser_automation": lambda args: args.get("action") == "navigate",
}


# task_scheduler 工具的合法动作集合
_TASK_SCHED_ACTIONS = {"create", "list", "delete", "pause", "resume"}
_TASK_SCHED_TYPES = {"cron", "interval", "once"}

# browser_automation 工具的合法动作集合
_BROWSER_ACTIONS = {"navigate", "click", "fill", "scroll", "snapshot", "wait_for", "screenshot", "close"}


class PythonASTAuditor(ast.NodeVisitor):
    """
    Layer 1 核心组件：基于抽象语法树(AST)的精细化行为审计器。
    不盲目封杀 os/sys 模块，而是动态拦截模块下的【高危逃逸与破坏性行为节点】。
    """

    def __init__(self):
        self.is_safe = True
        self.error_msg = ""

        self.forbidden_attributes = {
            'system', 'popen', 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe',
            'spawnv', 'spawnve', 'spawnvp', 'spawnvpe', 'execl', 'execle',
            'execlp', 'execpe', 'execv', 'execve', 'execvp', 'execvpe',
            'kill', 'chmod', 'chown', 'fork', 'ctypes'
        }
        self.forbidden_calls = {'eval', 'exec', 'compile', '__import__'}

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id in self.forbidden_calls:
                self.is_safe = False
                self.error_msg = f"静态 AST 拦截: 禁止使用动态执行函数 '{node.func.id}'，以防止沙箱审计逃逸。"
                return

        full_func_path = self._get_full_name(node.func)
        if full_func_path:
            parts = full_func_path.split('.')
            if any(p in self.forbidden_attributes for p in parts):
                self.is_safe = False
                self.error_msg = f"静态 AST 拦截: 检测到试图调用系统级高危函数或属性 '{full_func_path}'。"
                return
            if parts[0] == 'subprocess' or 'subprocess' in parts:
                self.is_safe = False
                self.error_msg = f"静态 AST 拦截: 禁止在 Python 沙盒中直接调用 'subprocess' 模块，请改用系统提供的 'bash_terminal' 工具。"
                return

        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name == 'subprocess':
                self.is_safe = False
                self.error_msg = "静态 AST 拦截: 禁止在沙箱内直接导入 'subprocess' 模块。"
                return
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module == 'subprocess':
            self.is_safe = False
            self.error_msg = "静态 AST 拦截: 禁止从 'subprocess' 导入内容。"
            return
        for alias in node.names:
            if alias.name in self.forbidden_attributes:
                self.is_safe = False
                self.error_msg = f"静态 AST 拦截: 禁止直接导入高危系统函数 '{alias.name}'。"
                return
        self.generic_visit(node)

    def _get_full_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            prefix = self._get_full_name(node.value)
            if prefix:
                return f"{prefix}.{node.attr}"
            return node.attr
        return None


class SecurityManager:
    """多维度安全审计拦截器（已解除 Session 强绑定，支持多沙箱工作区动态挂载）"""

    def __init__(self):
        self._audit_log: list = []
        self.whitelist = Whitelist()
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent.parent
        workspace_env = os.environ.get("WORKSPACE_ROOT")
        self.active_sandbox_roots: Set[Path] = set()
        if workspace_env:
            for p in workspace_env.split(","):
                if p.strip():
                    self.active_sandbox_roots.add(Path(p.strip()).resolve())
        else:
            self.active_sandbox_roots.add((project_root / "sandbox").resolve())

        for r in self.active_sandbox_roots:
            r.mkdir(parents=True, exist_ok=True)

        self.dangerous_shell_patterns = [
            r"\bsudo\b",
            r"\brm\b\s+-(?:[rRfF]+|.*\s+/[^\s]*)",
            r"\bmkfs\b",
            r"\bchmod\b\s+[70]77",
            r"\bchown\b",
            r">\s*/dev/(?:sd[a-z]|nvme)",
            r"\bcurl\b.*\|\s*(?:bash|sh)",
            r"\bwget\b.*\|\s*(?:bash|sh)",
            r"\bmv\b\s+.*\s+/(?:etc|usr|bin|lib|var)",
        ]

    def set_active_sandboxes(self, paths: list) -> None:
        """
        动态管理物理沙箱集群。
        1. 默认行为：追加（Add）新路径并自动去重。
        2. /sandbox reset : 一键重置回 .env 或默认底座配置。
        3. /sandbox remove 路径 : 从当前集群中移除特定沙箱。
        """
        if not paths:
            return
        if paths[0] == "reset":
            self.active_sandbox_roots.clear()
            workspace_env = os.environ.get("WORKSPACE_ROOT")
            if workspace_env:
                for p in workspace_env.split(","):
                    if p.strip():
                        self.active_sandbox_roots.add(Path(p.strip()).resolve())
            else:
                current_file = Path(__file__).resolve()
                project_root = current_file.parent.parent.parent.parent
                self.active_sandbox_roots.add((project_root / "sandbox").resolve())
            return
        if paths[0] in ("remove", "-r") and len(paths) > 1:
            for p in paths[1:]:
                resolved = Path(p.strip()).resolve()
                self.active_sandbox_roots.discard(resolved)
            return
        for p in paths:
            path_str = p.strip()
            if path_str:
                resolved = Path(path_str).resolve()
                resolved.mkdir(parents=True, exist_ok=True)
                self.active_sandbox_roots.add(resolved)

    def get_session_workspace(self, session_id: str) -> Path:
        """重构：解耦 Session，默认返回当前主工作区（第一个挂载点），用于放置临时执行脚本"""
        if self.active_sandbox_roots:
            return list(self.active_sandbox_roots)[0]
        return Path(".").resolve()

    def is_path_safe(self, target_path: Path) -> bool:
        """检查任意绝对路径是否落在当前已激活的任意一个沙箱工作区内"""
        try:
            resolved = target_path.resolve()
            for root in self.active_sandbox_roots:
                if resolved.is_relative_to(root):
                    return True
            return False
        except Exception:
            return False

    def _check_path_jail(self, target_path: str, session_id: str) -> Tuple[bool, str]:
        """重构路径越狱检查：多工作区全覆盖校验"""
        try:
            p = Path(target_path)
            if not p.is_absolute():
                resolved_path = (self.get_session_workspace(session_id) / p).resolve()
            else:
                resolved_path = p.resolve()
            if not self.is_path_safe(resolved_path):
                return False, f"Sandbox Violations: 目标路径 '{resolved_path}' 不在任何已激活的沙箱授信范围内！"
            return True, str(resolved_path)
        except Exception as e:
            return False, f"Path Resolution Error: ({str(e)})"

    def _validate_layer1_static(self, tool_name: str, input_data: Dict[str, Any], session_id: str) -> Tuple[bool, str]:
        if tool_name in ["read_file", "create_file", "edit_file"]:
            path_arg = input_data.get("file_path")
            if path_arg:
                safe, result_msg = self._check_path_jail(path_arg, session_id)
                if not safe: return False, result_msg
                input_data["file_path"] = result_msg

        elif tool_name in ["list_directory", "grep_search"]:
            path_arg = input_data.get("path", ".")
            safe, result_msg = self._check_path_jail(path_arg, session_id)
            if not safe: return False, result_msg
            input_data["path"] = result_msg

        elif tool_name == "python_interpreter":
            code_arg = input_data.get("code", "")
            if code_arg:
                try:
                    tree = ast.parse(code_arg)
                    auditor = PythonASTAuditor()
                    auditor.visit(tree)
                    if not auditor.is_safe: return False, auditor.error_msg
                except SyntaxError as se:
                    return False, f"AST 语法解析错误: 拒绝执行。详情: {str(se)}"

        elif tool_name == "bash_terminal":
            cmd_arg = input_data.get("command", "")
            if cmd_arg:
                for pattern in self.dangerous_shell_patterns:
                    if re.search(pattern, cmd_arg):
                        return False, f"高危 Shell 指令拦截: 不允许执行该指令: '{cmd_arg}'。"

        elif tool_name == "fuzzy_edit":
            return self._validate_fuzzy_edit(input_data, session_id)

        elif tool_name == "doc_fetch":
            return self._validate_doc_fetch(input_data)

        elif tool_name == "task_scheduler":
            return self._validate_task_scheduler(input_data)

        elif tool_name == "browser_automation":
            return self._validate_browser_automation(input_data, session_id)

        return True, ""

    # ============================================================
    # 新增 4 个工具的 Layer 1 静态校验（拆分为独立方法以控制嵌套深度）
    # ============================================================

    def _validate_fuzzy_edit(self, input_data: Dict[str, Any], session_id: str) -> Tuple[bool, str]:
        """模糊编辑工具：复用路径沙箱 + new_text 体积上限。"""
        path_arg = input_data.get("file_path")
        if not path_arg:
            return False, "fuzzy_edit 缺少必填参数 'file_path'。"

        safe, result_msg = self._check_path_jail(path_arg, session_id)
        if not safe:
            return False, result_msg
        input_data["file_path"] = result_msg

        max_kb = TOOL_QUOTA["fuzzy_edit"]["max_replace_size_kb"]
        max_bytes = max_kb * 1024
        new_text = input_data.get("new_text", "") or ""
        if len(new_text.encode("utf-8", errors="ignore")) > max_bytes:
            return False, f"fuzzy_edit 的 new_text 体积超过上限 {max_kb}KB，请拆分多次编辑。"
        return True, ""

    def _validate_doc_fetch(self, input_data: Dict[str, Any]) -> Tuple[bool, str]:
        """文档抓取工具：URL 协议白名单 + 黑名单 + max_pages 范围。"""
        url = (input_data.get("url") or "").strip()
        if not url:
            return False, "doc_fetch 缺少必填参数 'url'。"
        if not (url.startswith("http://") or url.startswith("https://")):
            return False, f"doc_fetch 仅支持 http/https URL，收到: '{url}'。"

        blocked, reason = self.whitelist.is_url_blocked(url)
        if blocked:
            return False, f"doc_fetch URL 拦截: {reason}"

        max_pages = input_data.get("max_pages", 1)
        try:
            max_pages_int = int(max_pages)
        except (TypeError, ValueError):
            return False, f"doc_fetch 的 max_pages 必须是整数，收到: {max_pages!r}。"
        if not (1 <= max_pages_int <= 500):
            return False, f"doc_fetch 的 max_pages 越界 (允许 [1,500])，收到: {max_pages_int}。"
        return True, ""

    def _validate_task_scheduler(self, input_data: Dict[str, Any]) -> Tuple[bool, str]:
        """定时任务工具：action 白名单 + create 动作的调度参数校验。"""
        action = input_data.get("action")
        if action not in _TASK_SCHED_ACTIONS:
            return False, f"task_scheduler 的 action 非法 '{action}'，允许: {sorted(_TASK_SCHED_ACTIONS)}。"

        if action != "create":
            return True, ""

        schedule_type = input_data.get("schedule_type")
        if schedule_type not in _TASK_SCHED_TYPES:
            return False, f"task_scheduler.create 的 schedule_type 非法 '{schedule_type}'，允许: {sorted(_TASK_SCHED_TYPES)}。"

        schedule_value = input_data.get("schedule_value")
        if schedule_type == "interval":
            return self._validate_interval_value(schedule_value)
        if schedule_type == "cron":
            return self._validate_cron_value(schedule_value)
        return True, ""

    def _validate_interval_value(self, schedule_value: Any) -> Tuple[bool, str]:
        """interval 模式：必须为整数秒，且 >= 最小间隔。"""
        min_seconds = TOOL_QUOTA["task_scheduler"]["min_interval_seconds"]
        try:
            seconds = int(schedule_value)
        except (TypeError, ValueError):
            return False, f"task_scheduler.interval 的 schedule_value 必须是整数秒数，收到: {schedule_value!r}。"
        if seconds < min_seconds:
            return False, f"task_scheduler.interval 不允许小于 {min_seconds} 秒的频率（防止任务风暴），收到: {seconds}。"
        return True, ""

    def _validate_cron_value(self, schedule_value: Any) -> Tuple[bool, str]:
        """cron 模式：能 import croniter 时校验语法，缺包静默放过。"""
        if not isinstance(schedule_value, str) or not schedule_value.strip():
            return False, "task_scheduler.cron 的 schedule_value 必须是非空字符串。"
        try:
            from croniter import croniter  # noqa: WPS433
        except ImportError:
            return True, ""  # 缺依赖时不阻塞，让工具自身报错
        if not croniter.is_valid(schedule_value):
            return False, f"task_scheduler.cron 表达式不合法: '{schedule_value}'。"
        return True, ""

    def _validate_browser_automation(self, input_data: Dict[str, Any], session_id: str) -> Tuple[bool, str]:
        """浏览器工具：action 白名单 + navigate URL 拦截 + screenshot 路径沙箱化。"""
        action = input_data.get("action")
        if action not in _BROWSER_ACTIONS:
            return False, f"browser_automation 的 action 非法 '{action}'，允许: {sorted(_BROWSER_ACTIONS)}。"

        if action == "navigate":
            url = (input_data.get("url") or "").strip()
            if not url:
                return False, "browser_automation.navigate 需要提供 'url'。"
            if not (url.startswith("http://") or url.startswith("https://")):
                return False, f"browser_automation 仅支持 http/https URL，收到: '{url}'。"
            blocked, reason = self.whitelist.is_url_blocked(url)
            if blocked:
                return False, f"browser_automation URL 拦截: {reason}"
            return True, ""

        if action == "screenshot":
            # 仅当显式指定输出路径时校验；默认走工具内部沙箱目录。
            path_arg = input_data.get("path") or input_data.get("file_path")
            if path_arg:
                safe, result_msg = self._check_path_jail(path_arg, session_id)
                if not safe:
                    return False, result_msg
        return True, ""

    def _llm_semantic_audit(self, tool_name: str, input_data: Dict[str, Any]) -> Tuple[int, str]:
        """
        ============================================================
        LAYER 2: 语义意图审查层（已全面重构为官方 OpenAI SDK 调用）
        ============================================================
        """
        try:
            # TODO(三模型差异化): 后续可切换为 get_small_config() / get_medium_config()
            config = get_main_config()
            if not config or not config.get("api_key"):
                return 75, "系统未配置专属语义审计模型配置，安全策略平滑降级转向人工终审。"

            prompt = f"""你是一个极其严厉的系统安全专家大模型（Critic Guard）。目前有一个开发智能体（AI Agent）正在运行并尝试调用物理系统工具。
请根据其调用的物理工具名称和传入的参数明细，深入推导出其背后的【最终业务语义意图】。你需要提防复杂的命令变形、反射代码拼凑等潜在沙箱逃逸或社会工程学越权。

【当前请求上下文】
- 工具名称: {tool_name}
- 参数内容: {json.dumps(input_data, ensure_ascii=False, indent=2)}

【审计规则】
1. 允许并鼓励正当的本地文件编写、合理的系统属性检查（如检查 python 版本、拼接良性沙箱路径）。
2. 你必须返回一个 [0 - 100] 区间的安全信任分数（Score）：
   - 90 至 100 分: 绝对安全。属于无害的纯业务逻辑代码、纯文件浏览。
   - 60 至 89 分: 灰色地带。静态规则未违规，但存在潜在的环境大范围变更、复杂的系统调用或批量文件覆写，需交付人工确认。
   - 0 至 59 分: 明确高危。检测到了明显的代码绕过隐蔽手段、越权读取或逃逸意图。

请严格按照以下唯一的 JSON 对象格式进行回复，绝对不能带有任何外部 markdown 标记：
{{
    "score": 评分数字,
    "reason": "详实清晰的风险理由及意图推导说明"
}}
"""
            # 初始化官方 OpenAI 同步客户端，内置 3 次自动指数退避重试
            client = OpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                max_retries=3
            )

            extra_body = {
                "thinking": {"type": "disabled"},
                "enable_thinking": False,
                "chat_template_kwargs": {"thinking": False}
            }

            # 调用官方 Completion 接口
            response = client.chat.completions.create(
                model=config["model_name"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},  # 强制要求模型输出严格的 JSON
                extra_body = extra_body
            )

            content = response.choices[0].message.content.strip()
            audit_res = json.loads(content)
            return int(audit_res.get("score", 75)), audit_res.get("reason", "未提供理由")

        except Exception as e:
            return 75, f"语义审计层运行异常 ({str(e)})，触发默认中度风险等级保护，转至人工终审。"

    def _human_audit(self, tool_name: str, input_data: Dict[str, Any], reason: str) -> bool:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
        from prompt_toolkit import prompt
        from prompt_toolkit.formatted_text import HTML
        console = Console()
        warning_text = Text()
        warning_text.append(f"👉 动作触点工具: {tool_name}\n", style="bold yellow")
        warning_text.append(f"🧠 审计层推导原因: {reason}\n\n", style="white")
        warning_text.append("📊 待执行的工具参数详情:\n", style="white")
        warning_text.append(json.dumps(input_data, ensure_ascii=False, indent=4), style="dim")
        console.print(Panel(
            warning_text,
            title="[bold red]🚨 [安全托管] 物理工具调用处于【灰色风险地带】",
            border_style="red",
            padding=(1, 2)
        ))
        while True:
            try:
                choice = prompt(
                    HTML("<ansicyan>❓ 是否允许 Agent 执行此项操作？[Y] 允许放行 / [N] 阻断并通知 Agent: </ansicyan>")
                ).strip().lower()
                if choice == "y":
                    return True
                elif choice == "n":
                    return False
                else:
                    console.print("[red]错误输入！请明确输入 Y 或 N。[/red]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[red]操作被用户中止，已默认阻断并通知 Agent。[/red]")
                return False

    def audit_log(self, action: str, tool_name: str, user_id: str, result: str) -> None:
        self._audit_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "tool_name": tool_name,
            "user_id": user_id,
            "result": result
        })

    def intercept(self, tool_name: str, input_data: Dict[str, Any], user_id: str = "default") -> Tuple[
        bool, Optional[str]]:
        self.audit_log("receive", tool_name, user_id, "incoming check")

        # ====== 1. LAYER 1: 确定性静态防御 ======
        l1_safe, l1_msg = self._validate_layer1_static(tool_name, input_data, session_id=user_id)
        if not l1_safe:
            self.audit_log("deny", tool_name, user_id, f"Layer 1 Blocked: {l1_msg}")
            return False, f"[Security Blocked] 静态防御拦截: {l1_msg}。"

        # ====== 2. LAYER 2: 语义意图审查（动作粒度可配置） ======
        should_layer2 = (
            tool_name in LAYER2_TARGETS
            and LAYER2_TARGETS[tool_name](input_data)
        )
        if should_layer2:
            score, reason = self._llm_semantic_audit(tool_name, input_data)

            if score < 60:
                self.audit_log("deny", tool_name, user_id, f"Layer 2 Blocked (Score {score}): {reason}")
                return False, f"[Security Blocked] 语义审计层拦截 (风险评分: {score}): {reason}。"

            # 3. 进入灰色风险，托管至 LAYER 3 人工终审
            if 60 <= score < 90:
                self.audit_log("suspend", tool_name, user_id, f"Layer 2 Suspended (Score {score}): {reason}")

                human_passed = self._human_audit(tool_name, input_data, reason)
                if not human_passed:
                    self.audit_log("deny", tool_name, user_id, "Layer 3 Human Denied")

                    # 联动触发长期记忆自愈提炼
                    try:
                        from harness_lite.memory.manager import MemoryManager
                        mem_manager = MemoryManager()

                        failed_command = ""
                        if tool_name == "bash_terminal":
                            failed_command = input_data.get("command", "")
                        elif tool_name == "python_interpreter":
                            failed_command = input_data.get("code", "")
                        else:
                            failed_command = json.dumps(input_data, ensure_ascii=False)

                        correction_context = f"人类用户在 Layer 3 交互界面明确按了 [N] 键拒绝放行。拦截理由: {reason}"

                        mem_manager.distill_and_record_correction(
                            session_id=user_id,
                            failed_command=failed_command,
                            correction_context=correction_context
                        )
                    except Exception as e:
                        print(f"[Memory System Warning] 触发纠错记忆动态提炼时发生异常: {str(e)}")

                    return False, f"[Security Blocked] [User Interrupted] 人类用户在最终审查层强制拒绝了执行。原因: 该操作具有潜在系统环境隐患。请更换一种更加温和、不触及敏感状态的全新策略来实现目标。"

        self.audit_log("allow", tool_name, user_id, "passed all layers")
        return True, None


security_manager = SecurityManager()