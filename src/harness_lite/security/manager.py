import os
import re
import ast
import json
import httpx
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from datetime import datetime
from harness_lite.config.loader import get_llm_config

class PythonASTAuditor(ast.NodeVisitor):
    """
    Layer 1 核心组件：基于抽象语法树(AST)的精细化行为审计器。
    不盲目封杀 os/sys 模块，而是动态拦截模块下的【高危逃逸与破坏性行为节点】。
    """
    def __init__(self):
        self.is_safe = True
        self.error_msg = ""

        # 明确禁止调用的高危系统函数或属性（无论通过何种别名导入，只要名称匹配即拦截）
        self.forbidden_attributes = {
            'system', 'popen', 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe',
            'spawnv', 'spawnve', 'spawnvp', 'spawnvpe', 'execl', 'execle',
            'execlp', 'execpe', 'execv', 'execve', 'execvp', 'execvpe',
            'kill', 'chmod', 'chown', 'fork', 'ctypes'
        }
        # 明确禁止在 Python 沙盒中直接调用的不受控子进程/动态执行函数
        self.forbidden_calls = {'eval', 'exec', 'compile', '__import__'}

    def visit_call(self, node):
        # 1. 拦截直接调用动态执行的内建函数 (如 eval, exec)，防止恶意拼凑绕过静态代码审查
        if isinstance(node.func, ast.Name):
            if node.func.id in self.forbidden_calls:
                self.is_safe = False
                self.error_msg = f"静态 AST 拦截: 禁止使用动态执行函数 '{node.func.id}'，以防止沙箱审计逃逸。"
                return

        # 2. 检查对象属性链条调用 (如 os.system, subprocess.run)
        full_func_path = self._get_full_name(node.func)
        if full_func_path:
            parts = full_func_path.split(".")
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
        # 拦截 subprocess 等底层高危模块的直接导入
        if node.module == 'subprocess':
            self.is_safe = False
            self.error_msg = "静态 AST 拦截: 禁止在沙箱内直接导入 'subprocess' 模块。"
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        # 拦截从任何模块导入危险函数（例如 from os import system）
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
        """
        递归展开解析形如 os.path.join 或 os.system 的完整调用链
        """
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            prefix = self._get_full_name(node.value)
            if prefix:
                return f"{prefix}.{node.attr}"
            return node.attr
        return None

class SecurityManager:
    """
    安全管理器：负责全链路多层防御（静态硬防 -> 语义推导 -> 人工终审）与 Session 沙箱隔离
    """

    def __init__(self):
        self._audit_log: list = []

        # 锁定主工作区根目录
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent.parent
        workspace_env = os.environ.get("WORKSPACE_ROOT")
        if workspace_env:
            self.base_workspace_root = Path(workspace_env).resolve()
        else:
            self.base_workspace_root = (project_root / "sandbox").resolve()
        self.base_workspace_root.mkdir(parents=True, exist_ok=True)

        # 高危 Shell 指令静态正则特征库 (Layer 1 辅助)
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

    def get_session_workspace(self, session_id: str) -> Path:
        """
        根据 session_id 动态生成并锁定专属于该会话的隔离工作区，防止跨 Session 数据越权泄露
        """
        session_root = (self.base_workspace_root / f"session_{session_id}").resolve()
        session_root.mkdir(parents=True, exist_ok=True)
        return session_root

    def _check_path_jail(self, target_path: str, session_id: str) -> Tuple[bool, str]:
        """
        Layer 1: 沙箱路径越界静态核验。强制将相对/绝对路径锁死在 Session 工作区内
        """
        try:
            session_root = self.get_session_workspace(session_id)
            p = Path(target_path)

            if not p.is_absolute():
                resolved_path = (session_root / p).resolve()
            else:
                resolved_path = p.resolve()

            if not resolved_path.is_relative_to(session_root):
                return False, f"Sandbox Violations: 尝试访问 Session 沙箱外部路径 '{resolved_path}'。你已被安全限制在专属目录内。"

            return True, str(resolved_path)

        except Exception as e:
            return False, f"Path Resolution Error: 路径解析异常 ({str(e)})"

    def _validate_layer1_static(self, tool_name: str, input_data: Dict[str, Any], session_id: str) -> Tuple[bool, str]:
        """
        ============================================================
        LAYER 1: 确定性静态防御层（静态规则速断 + AST 细粒度审计）
        ============================================================
        """

        if tool_name in ["read_file", "create_file", "edit_file"]:
            path_arg = input_data.get("file_path")
            if path_arg:
                safe, result_msg = self._check_path_jail(path_arg, session_id)
                if not safe:
                    return False, result_msg
                input_data["file_path"] = result_msg

        elif tool_name in ["list_directory", "grep_search"]:
            path_arg = input_data.get("path", ".")
            safe, result_msg = self._check_path_jail(path_arg, session_id)
            if not safe:
                return False, result_msg
            input_data["path"] = result_msg

        elif tool_name == "python_interpreter":
            code_arg = input_data.get("code", "")
            if code_arg:
                try:
                    tree = ast.parse(code_arg)
                    auditor = PythonASTAuditor()
                    auditor.visit(tree)
                    if not auditor.is_safe:
                        return False, auditor.error_msg
                except SyntaxError as se:
                    return False, f"AST 语法解析错误: 代码中存在语法异常，拒绝执行。详情: {str(se)}"

        elif tool_name == "bash_terminal":
            cmd_arg = input_data.get("command", "")
            if cmd_arg:
                for pattern in self.dangerous_shell_patterns:
                    if re.search(pattern, cmd_arg):
                        return False, f"高危 Shell 指令拦截: 核心策略拦截。不允许执行改变宿主机关键状态的指令: '{cmd_arg}'。"

        return True, ""

    def _llm_semantic_audit(self, tool_name: str, input_data: Dict[str, Any]) -> Tuple[int, str]:
        """
        ============================================================
        LAYER 2: 语义意图审查层（LLM Critic 深度推导兜底）
        ============================================================
        """

        try:
            config = get_llm_config()
            if not config or not  config.get("api_key"):
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
            
            请严格按照以下唯一的 JSON 对象格式进行回复，绝对不能带有任何外部 markdown 标记或闲聊杂质：
            {{
                "score": 评分数字,
                "reason": "详实清晰的风险理由及意图推导说明"
            }}
            """
            with httpx.Client(timeout=6.0) as client:
                response = client.post(
                    f"{config['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {config['api_key']}"},
                    json={
                        "model": config["model_name"],
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1
                    }
                )
                if response.status_code == 200:
                    res_json = response.json()
                    content = res_json["choices"][0]["message"]["content"].strip()
                    if content.startswith("```json"):
                        content = content.split("```json")[1].split("```")[0].strip()
                    elif content.startswith("```"):
                        content = content.split("```")[1].split("```")[0].strip()

                    audit_res = json.loads(content)
                    return int(audit_res.get("score", 75)), audit_res.get("reason", "未提供理由")

        except Exception as e:
            return 75, f"语义审计层运行异常 ({str(e)})，触发默认中度风险等级保护，转至人工终审。"

        return 75, "语义意图未决，安全降级至人工终审。"

    def _human_audit(self, tool_name: str, input_data: Dict[str, Any], reason: str) -> bool:
        """
        ============================================================
        LAYER 3: 人工审查确认层 (Human-In-The-Loop 最后的安全闭环)
        ============================================================
        """
        print("\n" + "🚨" * 30)
        print("⚠️  [安全托管] 物理工具调用已被捕获，系统判定其处于【灰色风险地带】")
        print(f"👉 动作触点工具: \033[1;33m{tool_name}\033[0m")
        print(f"🧠 审计层推导原因: {reason}")
        print("📊 待执行的工具参数详情:")
        print(json.dumps(input_data, ensure_ascii=False, indent=4))
        print("🚨" * 30)

        while True:
            choice = input("❓ 是否允许 Agent 执行此项操作？[Y] 允许放行 / [N] 阻断并通知 Agent: ").strip().lower()
            if choice == 'y':
                print("✅ [人工授权放行] 操作通过终审。")
                return True
            elif choice == 'n':
                print("❌ [人工拒绝阻断] 操作已被人类用户驳回！")
                return False
            else:
                print("错误输入！请明确输入 Y（代表允许）或 N（代表拒绝）。")

    def audit_log(self, action: str, tool_name: str, user_id: str, result: str) -> None:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "tool_name": tool_name,
            "user_id": user_id,
            "result": result
        }
        self._audit_log.append(log_entry)

    def intercept(self, tool_name: str, input_data: Dict[str, Any], user_id: str = "default") -> Tuple[
        bool, Optional[str]]:
        """
        立体纵深拦截核心管线：Layer 1 -> Layer 2 -> Layer 3 流式漏斗验证
        """
        self.audit_log("receive", tool_name, user_id, "incoming check")

        # ====== 1. LAYER 1: 确定性静态防御（路径、正则、AST检查） ======
        l1_safe, l1_msg = self._validate_layer1_static(tool_name, input_data, session_id=user_id)
        if not l1_safe:
            self.audit_log("deny", tool_name, user_id, f"Layer 1 Blocked: {l1_msg}")
            return False, f"静态防御拦截: {l1_msg}。请修正你的参数结构或代码。"

        # ====== 2. LAYER 2: 语义意图审查 ======
        # 仅针对高动态执行工具（终端、Python沙盒）激活大模型语义安全打分
        if tool_name in ["bash_terminal", "python_interpreter"]:
            score, reason = self._llm_semantic_audit(tool_name, input_data)

            # 分数过低：直接阻断熔断
            if score < 60:
                self.audit_log("deny", tool_name, user_id, f"Layer 2 Blocked (Score {score}): {reason}")
                return False, f"语义审计层拦截 (风险评分: {score}): {reason}。系统断定此行为存在安全隐患。"

            # 3. 触发分值触点：进入灰色风险，托管至 LAYER 3 人工终审（HITL）
            if 60 <= score < 90:
                self.audit_log("suspend", tool_name, user_id, f"Layer 2 Suspended (Score {score}): {reason}")

                # ====== 3. LAYER 3: 人工终审 ======
                human_passed = self._human_audit(tool_name, input_data, reason)
                if not human_passed:
                    self.audit_log("deny", tool_name, user_id, "Layer 3 Human Denied")

                    # ========================================================
                    # 【🔥 核心无 Bug 修复点】完美打通人工纠错与长期自愈记忆模块
                    # ========================================================
                    try:
                        # 1. 方法内部动态导入 MemoryManager，彻底从根源上斩断 Python 的循环引用（Circular Import）
                        from harness_lite.memory.manager import MemoryManager

                        # 2. 实例化内存管理器，系统将自动定位到统一的存储根目录
                        mem_manager = MemoryManager()

                        # 3. 智能解析出当前大模型到底执行了什么被你按 N 驳回的危险输入动作
                        failed_command = ""
                        if tool_name == "bash_terminal":
                            failed_command = input_data.get("command", "")
                        elif tool_name == "python_interpreter":
                            failed_command = input_data.get("code", "")
                        else:
                            failed_command = json.dumps(input_data, ensure_ascii=False)

                        # 4. 组装提炼所需的精确纠错上下文
                        correction_context = f"人类用户在 Layer 3 交互界面明确按了 [N] 键拒绝放行。拦截理由: {reason}"

                        # 5. 触发后台蒸馏引擎，将本次教训沉淀进长效跨会话的 Markdown 备忘录中
                        mem_manager.distill_and_record_correction(
                            session_id=user_id,  # 这里传入当前带有时间戳的会话ID，用于追溯短期日志
                            failed_command=failed_command,
                            correction_context=correction_context
                        )

                    except Exception as e:
                        # 记忆提炼系统在架构上设计为非阻塞辅助，一旦发生未知异常，记录警报，绝不破坏安全机制的熔断返回
                        print(f"[Memory System Warning] 触发纠错记忆动态提炼时发生异常: {str(e)}")

                    # 返回一条具备良好反哺特征的错误提示，引导大模型在当前会话的下一步推理中改过自新
                    return False, f"[User Interrupted] 人类用户在最终审查层强制拒绝了执行。原因: 该操作具有潜在系统环境隐患。请更换一种更加温和、不触及敏感状态的全新策略来实现目标。"

        self.audit_log("allow", tool_name, user_id, "passed all layers")
        return True, None

security_manager = SecurityManager()
