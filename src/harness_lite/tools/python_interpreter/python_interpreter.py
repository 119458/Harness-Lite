"""会话沙箱内的 Python 解释器工具"""

import os
import subprocess
import tempfile
from typing import Dict, Any

try:
    import resource
except ImportError:
    resource = None

from harness_lite.tools.base import BaseTool
from harness_lite.security.manager import security_manager
from harness_lite.tools.bash_terminal.process_manager import current_session_id


class PythonInterpreterTool(BaseTool):
    """
    多租户资源物理配额受控的 Python 交互式沙盒
    """

    @property
    def name(self) -> str:
        return "python_interpreter"

    @property
    def description(self) -> str:
        return "Python 代码沙盒环境。允许编写并执行一小段 Python 脚本（支持导入 os/sys 进行无害的路径及环境处理），并通过 print() 打印结果。"

    def __init__(self, timeout: int = 10):
        super().__init__()
        self.timeout = timeout

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "code": {
                "type": "string",
                "description": "要执行的 Python 脚本代码。注意：必须通过 print() 函数打印结果才能被外界看到。"
            }
        }
        schema["function"]["parameters"]["required"] = ["code"]
        return schema

    def _set_python_limits(self):
        """为临时的 Python 解释器注入独立的资源硬配额"""
        if not resource: return
        try:
            # 限制交互式脚本的最大内存，防止恶意内存泄露死循环
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            # 限制最大 CPU 物理执行秒数
            resource.setrlimit(resource.RLIMIT_CPU, (self.timeout + 1, self.timeout + 1))
        except Exception:
            pass

    def execute(self, code: str) -> str:
        if not code.strip():
            return "Error: 代码为空。"

        session_id = current_session_id.get()
        # 强制将临时脚本的运行和生成锁定在当前租户专属的沙箱子文件夹内
        session_root = security_manager.get_session_workspace(session_id)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8",
                                         dir=session_root) as temp_file:
            temp_file.write(code)
            temp_file_path = temp_file.name

        try:
            result = subprocess.run(
                ['python', temp_file_path],
                cwd=str(session_root),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                preexec_fn=self._set_python_limits if os.name != 'nt' else None
            )
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout.strip()}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr.strip()}\n"

            if result.returncode == 0:
                return f"Execution Success:\n{output}".strip() if output else "Success: 代码运行完毕，但没有任何打印输出。"
            else:
                return f"Execution Failed (Exit code {result.returncode}):\n{output}".strip()

        except subprocess.TimeoutExpired:
            return f"Error: Python 代码执行超时 ({self.timeout}秒)。物理资源限制引擎已强制熔断该进程。可能存在死循环或长时间的网络请求。"
        except Exception as e:
            return f"System Error: {str(e)}"

        finally:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception:
                    pass
