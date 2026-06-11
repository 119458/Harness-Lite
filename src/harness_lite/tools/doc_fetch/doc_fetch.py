"""文档抓取工具：下载远程 PDF/Word/Excel/PPT 并解析为文本

设计要点：
- 仅支持 http/https，禁止 file://、ftp 等本地协议。
- 下载到沙箱内的临时目录，处理完后立即删除临时文件。
- 单文件硬上限 50 MB，超过则拒绝；下载过程中超限会主动中止并清理。
- 解析依赖（pypdf / python-docx / openpyxl / python-pptx）通过 try-import 加载，
  缺失时给出中文友好提示，不抛异常。
- 输出走 truncate_from_head，避免撑爆上下文。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urlparse

from harness_lite.tools.base import BaseTool
from harness_lite.tools.utils.output_truncate import (
    human_readable_size,
    truncate_from_head,
)

_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_TIMEOUT = 30  # 秒
_DEFAULT_MAX_PAGES = 50
_CHUNK_SIZE = 64 * 1024

_PARSER_REGISTRY = {
    ".pdf": "_extract_pdf",
    ".docx": "_extract_word",
    ".xlsx": "_extract_excel",
    ".xls": "_extract_excel",
    ".pptx": "_extract_pptx",
}

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


class DocFetchTool(BaseTool):
    """远程文档抓取与解析工具"""

    @property
    def name(self) -> str:
        return "doc_fetch"

    @property
    def description(self) -> str:
        return (
            "下载远程文档（PDF / Word / Excel / PPT）到沙箱内临时目录，"
            "解析其中文本内容并返回。支持 .pdf、.docx、.xls、.xlsx、.pptx 等扩展名，"
            "单文件硬上限 50MB。仅接受 http/https URL，不可访问本地文件或内网地址。"
            "纯 HTML 网页请改用 web_scraper。"
        )

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "url": {
                "type": "string",
                "description": "待抓取文档的完整 URL，必须以 http:// 或 https:// 开头。",
            },
            "max_pages": {
                "type": "integer",
                "description": f"最多解析的页数/工作表数/幻灯片数，默认 {_DEFAULT_MAX_PAGES}，仅对 PDF/PPT/Excel 生效。",
            },
        }
        schema["function"]["parameters"]["required"] = ["url"]
        return schema

    def execute(self, url: str, max_pages: int = _DEFAULT_MAX_PAGES) -> str:
        url = (url or "").strip()
        validate_err = self._validate_url(url)
        if validate_err:
            return validate_err

        suffix = self._detect_suffix(url)
        if suffix not in _PARSER_REGISTRY:
            return (
                f"Error: 不支持的扩展名 '{suffix or '未知'}'。"
                "本工具仅解析 PDF/Word/Excel/PPT，HTML 网页请改用 web_scraper。"
            )

        requests_mod = _safe_import("requests")
        if requests_mod is None:
            return "Error: 缺少 requests 依赖，请执行 `pip install requests` 后重试。"

        try:
            sandbox_tmp = self._prepare_tmp_dir()
        except OSError as exc:
            return f"Error: 无法在沙箱内创建临时目录（{exc}），已拒绝下载以避免越出沙箱。"
        local_file = sandbox_tmp / f"{uuid.uuid4().hex[:10]}_{self._safe_filename(url)}"
        download_err = self._download(requests_mod, url, local_file)
        if download_err:
            self._cleanup(local_file)
            return download_err

        try:
            parsed_text = self._dispatch_parser(local_file, suffix, max_pages)
        except _ParseError as err:
            return str(err)
        finally:
            self._cleanup(local_file)

        if not parsed_text or not parsed_text.strip():
            return f"[文档已下载但未提取到任何文本内容，可能是扫描版或加密文件] URL: {url}"

        truncated, was_truncated, reason = truncate_from_head(parsed_text)
        header = f"[来源: {url}]\n"
        if was_truncated:
            header += f"[已截断，触发原因={reason}]\n"
        return header + truncated

    # ---------- URL 校验与基础工具 ----------

    @staticmethod
    def _validate_url(url: str) -> str:
        if not url:
            return "Error: url 不能为空。"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Error: 仅支持 http/https 协议。"
        if not parsed.netloc:
            return "Error: URL 缺少域名部分。"
        return ""

    @staticmethod
    def _detect_suffix(url: str) -> str:
        path = urlparse(url).path
        return os.path.splitext(path)[1].lower()

    @staticmethod
    def _safe_filename(url: str) -> str:
        basename = os.path.basename(unquote(urlparse(url).path))
        if not basename or basename == "/":
            basename = "remote_document"
        # 仅保留安全字符
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in basename)
        return safe[:80] or "remote_document"

    def _prepare_tmp_dir(self) -> Path:
        """选择沙箱内的临时目录；失败直接抛 OSError，禁止越出沙箱"""
        try:
            from harness_lite.security.manager import security_manager  # 延迟导入避免循环
            base = security_manager.get_session_workspace("default")
        except Exception:
            workspace = os.environ.get("WORKSPACE_ROOT", "").split(",")[0].strip()
            base = Path(workspace) if workspace else Path.cwd()
        tmp_dir = Path(base) / "_doc_fetch_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)  # 失败直接抛 OSError，不降级到系统临时目录
        return tmp_dir

    # ---------- 下载 ----------

    def _download(self, requests_mod, url: str, target: Path) -> str:
        try:
            response = requests_mod.get(
                url,
                headers=_DEFAULT_HEADERS,
                timeout=_DOWNLOAD_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests_mod.Timeout:
            return f"Error: 下载超时（>{_DOWNLOAD_TIMEOUT}s）。"
        except requests_mod.ConnectionError as exc:
            return f"Error: 网络连接失败（{exc}）。"
        except requests_mod.HTTPError as exc:
            status = getattr(exc.response, "status_code", "?")
            return f"Error: HTTP {status} 错误，无法下载 {url}。"
        except Exception as exc:  # noqa: BLE001
            return f"Error: 请求异常（{exc}）。"

        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            if int(content_length) > _MAX_FILE_SIZE:
                return (
                    f"Error: 文件大小 {human_readable_size(int(content_length))} 超过 50MB 上限。"
                )

        downloaded = 0
        try:
            with open(target, "wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > _MAX_FILE_SIZE:
                        return (
                            f"Error: 下载过程中文件超过 50MB 上限，已主动中止。"
                        )
                    fh.write(chunk)
        except OSError as exc:
            return f"Error: 写入临时文件失败（{exc}）。"
        return ""

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    # ---------- 解析路由 ----------

    def _dispatch_parser(self, file_path: Path, suffix: str, max_pages: int) -> str:
        method_name = _PARSER_REGISTRY[suffix]
        parser: Callable[[Path, int], str] = getattr(self, method_name)
        return parser(file_path, max_pages)

    def _extract_pdf(self, file_path: Path, max_pages: int) -> str:
        pypdf = _safe_import("pypdf")
        if pypdf is None:
            raise _ParseError(
                "Error: 缺少 pypdf 依赖，请执行 `pip install pypdf` 后重试。"
            )
        try:
            reader = pypdf.PdfReader(str(file_path))
        except Exception as exc:  # noqa: BLE001
            raise _ParseError(f"Error: 无法解析 PDF（{exc}）。")
        total = len(reader.pages)
        limit = min(max_pages, total)
        segments = []
        for idx in range(limit):
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                text = f"(第 {idx + 1} 页解析失败：{exc})"
            if text.strip():
                segments.append(f"--- Page {idx + 1}/{total} ---\n{text}")
        if limit < total:
            segments.append(f"(已截断，仅解析前 {limit} 页，共 {total} 页)")
        return "\n\n".join(segments)

    def _extract_word(self, file_path: Path, _max_pages: int) -> str:
        docx_mod = _safe_import("docx")
        if docx_mod is None:
            raise _ParseError(
                "Error: 缺少 python-docx 依赖，请执行 `pip install python-docx` 后重试。"
            )
        try:
            document = docx_mod.Document(str(file_path))
        except Exception as exc:  # noqa: BLE001
            raise _ParseError(f"Error: 无法解析 Word 文档（{exc}）。")
        lines = [p.text for p in document.paragraphs if p.text.strip()]
        return "\n".join(lines)

    def _extract_excel(self, file_path: Path, max_pages: int) -> str:
        openpyxl = _safe_import("openpyxl")
        if openpyxl is None:
            raise _ParseError(
                "Error: 缺少 openpyxl 依赖，请执行 `pip install openpyxl` 后重试。"
            )
        try:
            workbook = openpyxl.load_workbook(str(file_path), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise _ParseError(f"Error: 无法解析 Excel 文档（{exc}）。")
        sheet_blocks = []
        try:
            sheet_names = workbook.sheetnames[:max_pages]
            for name in sheet_names:
                worksheet = workbook[name]
                rows_text = []
                for row in worksheet.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c) for c in row]
                    if any(cells):
                        rows_text.append(" | ".join(cells))
                if rows_text:
                    sheet_blocks.append(f"--- Sheet: {name} ---\n" + "\n".join(rows_text))
        finally:
            workbook.close()
        return "\n\n".join(sheet_blocks)

    def _extract_pptx(self, file_path: Path, max_pages: int) -> str:
        pptx = _safe_import("pptx")
        if pptx is None:
            raise _ParseError(
                "Error: 缺少 python-pptx 依赖，请执行 `pip install python-pptx` 后重试。"
            )
        try:
            presentation = pptx.Presentation(str(file_path))
        except Exception as exc:  # noqa: BLE001
            raise _ParseError(f"Error: 无法解析 PPT 文档（{exc}）。")
        total = len(presentation.slides)
        limit = min(max_pages, total)
        slide_blocks = []
        for idx, slide in enumerate(presentation.slides):
            if idx >= limit:
                break
            texts = []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    txt = paragraph.text.strip()
                    if txt:
                        texts.append(txt)
            if texts:
                slide_blocks.append(f"--- Slide {idx + 1}/{total} ---\n" + "\n".join(texts))
        if limit < total:
            slide_blocks.append(f"(已截断，仅解析前 {limit} 张幻灯片，共 {total} 张)")
        return "\n\n".join(slide_blocks)


class _ParseError(Exception):
    """解析阶段的可恢复错误，message 直接回传给 LLM"""


def _safe_import(module_name: str) -> Optional[Any]:
    try:
        return __import__(module_name)
    except ImportError:
        return None
