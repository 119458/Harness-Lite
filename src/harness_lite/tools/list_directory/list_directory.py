"""目录树形结构列表工具"""

from pathlib import Path
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class ListDirectoryTool(BaseTool):
    """
    查看目录结构工具，自带深度限制和无用目录过滤
    """
    @property
    def name(self) -> str:
        return "list_directory"

    # 2. 使用 property 实现 description
    @property
    def description(self) -> str:
        return "列出指定目录下的文件和文件夹结构。用于了解项目结构，自动过滤隐藏文件和缓存目录。"
    def __init__(self):
        super().__init__()
        # 预设过滤无用目录，防止污染 LLM 的上下文
        self.ignore_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode'}

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "path": {
                "type": "string",
                "description": "要列出的目录路径，默认为当前工作目录 '.'"
            },
            "max_depth": {
                "type": "integer",
                "description": "遍历的最大深度，默认 2。防止输出过长导致上下文溢出。"
            }
        }
        return schema

    def execute(self, path: str = ".", max_depth: int = 2) -> str:
        base_path = Path(path)
        if not base_path.exists() or not base_path.is_dir():
            return f"Error: 目录 '{path}' 不存在或不是一个有效的目录。"

        tree_str = []
        def _build_tree(current_path: Path, current_depth: int):
            if current_depth > max_depth:
                return

            try:
                items = sorted(current_path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
                for item in items:
                    if item.is_dir() and item.name in self.ignore_dirs:
                        continue
                    indent = "  " * current_depth
                    if item.is_dir():
                        tree_str.append(f"{indent}📂 {item.name}/")
                        _build_tree(item, current_depth + 1)
                    else:
                        tree_str.append(f"{indent}📄 {item.name}")
            except PermissionError:
                tree_str.append(f"{'  ' * current_depth}🔒 [Permission Denied]")

        tree_str.append(f"📂 {base_path.absolute()}")
        _build_tree(base_path, 1)

        return "\n".join(tree_str)
