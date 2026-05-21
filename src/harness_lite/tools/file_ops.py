import os
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

class ReadFileTool(BaseTool):
    """
    带有行号限制的文件读取工具，防止一次读取过大文件
    """
    @property
    def name(self) -> str:
        return "read_file"
    @property
    def description(self) -> str:
        return "读取本地文件的内容。强烈建议指定行号范围，以便在审查大文件时节省上下文。"
    def __init__(self):
        super().__init__()


    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要读取的文件绝对或相对路径"
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（从 1 开始），可选。默认 1。"
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号，可选。默认读取到文件末尾。"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path"]
        return schema

    def execute(self, file_path: str, start_line: int = 1, end_line: int = -1) -> str:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return f"Error: 文件 '{file_path}' 不存在。"

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = total_lines if end_line == -1 else min(total_lines, end_line)

            if start_idx >= total_lines:
                return f"Error: start_line ({start_line}) 超出了文件总行数 ({total_lines})。"

            content_lines = []
            for i in range(start_idx, end_idx):
                content_lines.append(f"{i + 1:4d} | {lines[i]}")

            return f"--- File: {file_path} (Lines: {start_idx + 1} to {end_idx}, Total: {total_lines}) ---\n" + "".join(content_lines)
        except UnicodeDecodeError:
            return f"Error: 文件 '{file_path}' 似乎是二进制文件，无法作为文本读取。"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class CreateFileTool(BaseTool):
    """
    安全的文件创建工具
    """
    @property
    def name(self) -> str:
        return "create_file"
    @property
    def description(self) -> str:
        return "创建一个新文件并写入初始内容。如果文件已存在，将拒绝操作以防止覆盖已有代码。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要创建的文件路径"
            },
            "content": {
                "type": "string",
                "description": "新文件的初始内容"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path", "content"]
        return schema

    def execute(self, file_path: str, content: str = "") -> str:
        path = Path(file_path)
        if path.exists():
            return f"Error: 文件 '{file_path}' 已存在。请使用 edit_file 工具进行局部修改，或换一个文件名。"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Success: 文件 '{file_path}' 已成功创建并写入内容。"
        except Exception as e:
            return f"Error creating file: {str(e)}"


class EditFileTool(BaseTool):
    """
    基于行号区间的精确文件修改工具 (Line-based Replacer)
    """
    @property
    def name(self) -> str:
        return "edit_file"
    @property
    def description(self) -> str:
        return "修改文件的指定行号区间。你需要提供起始行、结束行以及替换的新内容。建议修改前先用 read_file 工具确认准确的行号。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要修改的文件路径 (相对沙箱根目录)"
            },
            "start_line": {
                "type": "integer",
                "description": "要替换的起始行号（包含，从 1 开始）。如果要插入到文件头部，请设为 1。"
            },
            "end_line": {
                "type": "integer",
                "description": "要替换的结束行号（包含）。如果要删除这些行，将 new_content 留空即可。"
            },
            "new_content": {
                "type": "string",
                "description": "替换后的新代码内容。请确保包含正确的缩进和换行符。"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path", "start_line", "end_line", "new_content"]
        return schema

    def execute(self, file_path: str, start_line: int, end_line: int, new_content: str) -> str:
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(os.getcwd()) / path

        if not path.exists() or not path.is_file():
            return f"Error: 文件 '{file_path}' 不存在。请确认路径是否正确。"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) 不能大于 end_line ({end_line})。"

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            total_lines = len(lines)

            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, end_line)

            if new_content and not new_content.endswith("\n"):
                new_content += "\n"

            new_lines = lines[: start_idx] + ([new_content] if new_content else []) + lines[end_idx:]

            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            return f"Success: 已将 '{file_path}' 的第 {start_line} 到 {end_line} 行替换为新内容。当前文件总行数变为 {len(new_lines)} 行。"
        except Exception as e:
            return f"Error editing file: {str(e)}"

class GrepSearchTool(BaseTool):
    """
    全局内容检索工具
    """
    @property
    def name(self) -> str:
        return "grep_search"
    @property
    def description(self) -> str:
        return "在指定目录下递归搜索包含特定关键字的文件及行号。用于快速定位函数定义或变量。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "query": {
                "type": "string",
                "description": "要搜索的关键字或字符串"
            },
            "path": {
                "type": "string",
                "description": "搜索的起始目录，默认为当前目录 '.'"
            }
        }
        schema["function"]["parameters"]["required"] = ["query"]
        return schema

    def execute(self, query: str, path: str = ".") -> str:
        base_path = Path(path)
        if not base_path.exists() or not base_path.is_dir():
            return f"Error: 目录 '{path}' 不存在。"

        ignore_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}
        results = []

        try:
            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for file in files:
                    if file.endswith(('.png', '.jpg', '.jpeg', '.pyc', '.pdf', '.zip', '.tar', '.gz')):
                        continue

                    file_path = Path(root) / file
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            for i, line in enumerate(f):
                                if query in line:
                                    results.append(f"{file_path}:{i+1}: {line.strip()}")
                                    if len(results) > 50:
                                        return "\n".join(results) + f"\n\n... (超过 50 条结果，已截断。请使用更具体的 query)"
                    except (UnicodeDecodeError, PermissionError):
                        continue

            if not results:
                return f"No results found for '{query}' in directory '{path}'"
            return "\n".join(results)
        except Exception as e:
            return f"Error during search: {str(e)}"