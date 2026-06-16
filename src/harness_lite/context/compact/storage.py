"""L1 大结果落盘层。

把超过阈值的 tool 消息内容物理移到磁盘，
内存中只留一段含预览与 ref_id 的存根，从而：
- 让 L3 时间衰减扫描的有效负载骤降；
- 让 L5 摘要 LLM 输入更精炼，省 token、省钱、提质量。

路径布局：`{base_dir}/large_results/{session_id}/{ref_id}.txt`
对 session_id 与 ref_id 都做了严格的路径越界防护。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness_lite.context.compact.types import ToolResultRef

logger = logging.getLogger("harness_lite.compact")


LARGE_RESULT_THRESHOLD_BYTES = 50_000
LARGE_RESULT_PREVIEW_CHARS = 800
LARGE_RESULT_STUB_PREFIX = "[⚠️ 大结果已自动归档]"


_REF_ID_PATTERN = re.compile(r"[a-f0-9]+")


def _sanitize_session_id(session_id: str) -> str:
    """与 memory/store.py:_get_file_path 保持同样的 session_id 净化策略。"""
    sid = str(session_id) if not isinstance(session_id, str) else session_id
    if not sid:
        raise ValueError("session_id 不能为空")
    sid = sid.strip()
    if not sid:
        raise ValueError("session_id 不能为空或纯空白")
    sid = sid.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    if sid == ".." or sid == "." or set(sid) == {"."}:
        raise ValueError(f"session_id 非法: {sid!r}")
    return sid


def _validate_ref_id(ref_id: str) -> str:
    """ref_id 必须是十六进制字符串（理论上 sha256 截断必然成立，仍校验防注入）。"""
    if not ref_id or not _REF_ID_PATTERN.fullmatch(ref_id):
        raise ValueError(f"非法 ref_id: {ref_id!r}")
    return ref_id


def _ensure_inside(root: Path, target: Path) -> None:
    """用 os.path.commonpath 确认 target 在 root 之下，防 path traversal。"""
    try:
        common = os.path.commonpath([str(root), str(target)])
    except ValueError as exc:  # 不同盘符 / 完全不相关 → commonpath 抛 ValueError
        raise ValueError(f"路径越界：{target} 不在 {root} 下") from exc
    if Path(common).resolve() != root.resolve():
        raise ValueError(f"路径越界：{target} 不在 {root} 下")


class LargeResultStore:
    """大结果磁盘存储。

    所有公开方法遵守同一组安全约束：
    - session_id 经 `_sanitize_session_id` 后再拼路径；
    - ref_id 必须是十六进制；
    - 最终路径用 `_ensure_inside` 校验仍在 `self.root` 下。
    """

    def __init__(self, base_dir: Path = Path("./memory_store")):
        self.base_dir = Path(base_dir)
        self.root = (self.base_dir / "large_results").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        safe_sid = _sanitize_session_id(session_id)
        target = (self.root / safe_sid).resolve()
        _ensure_inside(self.root, target)
        return target

    def _file_path(self, session_id: str, ref_id: str) -> Path:
        _validate_ref_id(ref_id)
        session_dir = self._session_dir(session_id)
        target = (session_dir / f"{ref_id}.txt").resolve()
        _ensure_inside(self.root, target)
        return target

    def write(self, session_id: str, ref_id: str, content: str) -> Path:
        """落盘大结果；编码异常自动 replace 防崩溃。"""
        path = self._file_path(session_id, ref_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)
        logger.debug(
            "LargeResultStore.write session=%s ref_id=%s bytes=%d",
            session_id, ref_id, len(content.encode("utf-8", errors="replace")),
        )
        return path

    def read(self, session_id: str, ref_id: str) -> Optional[str]:
        """读取落盘内容；文件不存在返回 None，不抛异常。"""
        path = self._file_path(session_id, ref_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            logger.warning("LargeResultStore.read 读取失败: %s", exc)
            return None

    def cleanup_session(self, session_id: str) -> None:
        """删除整个 session 子目录；与 memory/manager.py 的清理风格一致。"""
        session_dir = self._session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.debug("LargeResultStore.cleanup_session 已清理 %s", session_dir)

    def list_session_files(self, session_id: str) -> List[Path]:
        """枚举该 session 下所有归档文件，便于 CLI 诊断。"""
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return []
        return sorted(p for p in session_dir.glob("*.txt") if p.is_file())


class DiskOffloadLayer:
    """L1：tool 消息超阈即时落盘 + 改写为存根。

    幂等保证：检测 content 已经以 `LARGE_RESULT_STUB_PREFIX` 开头则视为已归档，跳过重写。
    """

    def __init__(self, store: LargeResultStore):
        self.store = store

    def maybe_offload(
        self,
        message: Dict[str, Any],
        session_id: str,
    ) -> Tuple[Dict[str, Any], Optional[ToolResultRef]]:
        """对单条 tool 消息按需落盘。

        返回值：
        - (改写后消息, ToolResultRef) 表示发生了归档；
        - (原消息, None) 表示无需归档（非 tool / 体积不够 / 已是存根）。
        """
        if message.get("role") != "tool":
            return message, None

        content = message.get("content", "") or ""
        if not isinstance(content, str):
            return message, None

        # 幂等：已经是存根直接返回
        if content.startswith(LARGE_RESULT_STUB_PREFIX):
            return message, None

        encoded = content.encode("utf-8", errors="replace")
        byte_size = len(encoded)
        if byte_size < LARGE_RESULT_THRESHOLD_BYTES:
            return message, None

        tool_call_id = message.get("tool_call_id", "") or ""
        if not tool_call_id:
            # 没有 tool_call_id 无法稳定生成 ref_id，放弃落盘
            logger.warning("DiskOffloadLayer: tool 消息缺少 tool_call_id，跳过归档")
            return message, None

        content_hash = hashlib.sha256(encoded).hexdigest()
        ref_id = hashlib.sha256(
            (tool_call_id + content_hash).encode("utf-8")
        ).hexdigest()[:16]

        try:
            disk_path = self.store.write(session_id, ref_id, content)
        except (ValueError, OSError) as exc:
            logger.error("DiskOffloadLayer.write 失败: %s", exc)
            return message, None

        preview = content[:LARGE_RESULT_PREVIEW_CHARS]
        new_content = (
            f"{LARGE_RESULT_STUB_PREFIX} tool_call_id={tool_call_id} "
            f"ref_id={ref_id} byte_size={byte_size}\n"
            f"─── 原始输出前 {LARGE_RESULT_PREVIEW_CHARS} 字预览 ───\n"
            f"{preview}\n"
            f"─── 已截断，剩余字符落盘至 large_results/{_sanitize_session_id(session_id)}/{ref_id}.txt ───"
        )
        new_message = dict(message)
        new_message["content"] = new_content

        ref = ToolResultRef(
            ref_id=ref_id,
            tool_call_id=tool_call_id,
            tool_name=message.get("name", "") or "unknown",
            disk_path=str(disk_path),
            content_hash=content_hash,
            byte_size=byte_size,
            preview=preview,
        )
        logger.info(
            "DiskOffloadLayer 已归档 tool_call_id=%s ref_id=%s bytes=%d",
            tool_call_id, ref_id, byte_size,
        )
        return new_message, ref
