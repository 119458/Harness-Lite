"""长期记忆系统 v2：后台抽取 + 索引常驻 + 按需召回 + 类型语义分层。

核心改造：
- 4 类记忆 (user/feedback/project/reference) 含中文标签与差异化正文约束
- system prompt 仅装载 行为指南 + MEMORY.md 索引（不预取推荐清单）
- 写入由后台 daemon 抽取 agent 完成，不分散主模型注意力
- 召回仅在主模型决定调用 tool_calls 时由 strategy 在工具执行期间并行触发
- 仅 project 类记忆强制 created_at + STALE 警告 (>2 天)
- 会话级 read_file 已读去重，避免推荐已读条目
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from harness_lite.config.loader import get_small_config, get_main_config
from harness_lite.memory.manager import _fire_invalidation

logger = logging.getLogger("harness_lite.memory.long_term")

# ============================================================
# 模块级常量
# ============================================================

MAX_FILES = 200
FRONTMATTER_MAX_LINES = 30
BODY_EXCERPT_MAX_LINES = 20
MEMORY_DESCRIPTION_MAX_CHARS = 80
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000

MEMORY_TYPES = ("user", "feedback", "project", "reference")

TYPE_LABEL_CN = {
    "user": "用户画像",
    "feedback": "偏好反馈",
    "project": "项目动态",
    "reference": "外部指针",
}

# project 类记忆的陈旧度阈值（天），可由环境变量覆盖
def _load_stale_threshold() -> int:
    raw = os.environ.get("LTM_STALE_THRESHOLD_DAYS")
    if not raw:
        return 2
    try:
        v = int(raw)
        return v if v > 0 else 2
    except (TypeError, ValueError):
        return 2

STALE_THRESHOLD_DAYS = _load_stale_threshold()

# 后台抽取触发阈值（任一满足即触发）
EXTRACT_TRIGGER_USER_TURNS = 3
EXTRACT_TRIGGER_TOOL_CALLS = 10
EXTRACT_TRIGGER_CHARS = 8000
EXTRACT_MAX_MEMORIES_PER_CALL = 3
HEAVY_TURN_TOOL_THRESHOLD = 20

RECALL_TOP_K = 5
RECENT_TOOLS_WINDOW = 10
RECALL_MAX_CHARS_PER_MEMORY = 8000
RECALL_MAX_TOTAL_CHARS = 24000

# ============================================================
# 提示词（中文，严苛优先）
# ============================================================

_SELECT_PROMPT_CN = """你是一个长期记忆筛选器。下面会给你用户当前的查询和一份候选记忆清单（仅文件名和一行描述）。

请你**只选你确信对回答查询有帮助的记忆**。规则：

1. **宁可少选，不可错选**。如果你不确定某条记忆是否相关，就**不选**。
2. **最多 5 条**。如果只有 2 条真正相关，就只返回 2 条；如果没有任何明显相关，**返回空列表**。
3. 仅根据 filename + description 判断，不要靠想象补全正文细节。
4. 「最近使用的工具」列表中的工具，**不要**选择标题/描述读起来像「用法说明、参考文档、API 文档」的记忆——智能体当前对话里已在用它，再塞一份用法文档是噪音。
5. 同一工具的「告警、坑点、已知问题、踩坑教训」类记忆仍要选——这恰恰是它正在用时最该出现的提醒。

严格输出 JSON：{"selected": ["filename1.md", ...]}"""


_EXTRACTION_PROMPT_CN = """# 记忆抽取助手

你正在事后回看一段刚结束的对话。请只抽取**真正值得长期保留的事实**——
即使下一周、下个月看到这条记忆，对智能体协助用户仍然有用的内容。

抽取的记忆必须落入下面 4 类之一：

- **用户画像 (user)**：用户的身份、职业、擅长领域、长期目标。一次性请求不算。
- **偏好反馈 (feedback)**：用户对协作方式的稳定偏好或明确禁忌。一次性纠错不算。
- **项目动态 (project)**：项目目标、阶段决定、外部约束、未决问题。代码细节不算。
- **外部指针 (reference)**：文档链接、知识源、外部工具入口。本地路径不算。

**严格不要保存：**
- 代码片段、文件路径、目录结构（项目内 grep 即可获得）
- git 历史、commit、变更人（git log 是权威）
- 单次调试方案或临时修复
- 已经写在项目规范文档里的内容
- 进行中的临时任务状态

如果本轮对话没有值得长期保留的事实，请返回 `{"memories": []}`。

重要的类型约束：
- 当 type == "feedback" 或 "project" 时，content 字段**必须**包含以下两段（Markdown 加粗）：
  **Why:** 用户为什么这么要求 / 项目为什么有这个状态（背后的原因，往往是踩过的坑）
  **How to apply:** 在什么情况下这条规则/状态生效（清晰的触发条件）
- 当 type == "project" 时，必须保证内容含明确的项目当前状态/决定/截止日期，便于将来判断陈旧度。
- 当 type == "user" 或 "reference" 时，正文按自然语言自由组织即可，不强制结构。

每次抽取最多产出 3 条记忆。

输出严格 JSON：
{
  "memories": [
    {
      "type": "user 或 feedback 或 project 或 reference",
      "name": "短横线短名,作为文件主键,英文小写",
      "description": "一行 ≤80 字描述,用于未来召回时判断相关性",
      "content": "正文 Markdown"
    }
  ]
}"""


# ============================================================
# 数据结构
# ============================================================

@dataclass
class MemoryHeader:
    """单个记忆文件的元数据结构。"""
    filename: str
    file_path: str
    mtime_ms: float
    description: Optional[str]
    type: Optional[str]
    name: Optional[str] = None
    created_at: Optional[str] = None  # YYYY-MM-DD
    updated_at: Optional[str] = None  # YYYY-MM-DD


@dataclass
class _ExtractionCounter:
    """信息量累积计数器（按 session 维度）。"""
    pending_user_turns: int = 0
    pending_tool_calls: int = 0
    pending_chars: int = 0
    last_extract_at: float = 0.0
    last_message_count: int = 0  # 记录已统计到的 messages 长度，避免重复累加

    def reset(self) -> None:
        self.pending_user_turns = 0
        self.pending_tool_calls = 0
        self.pending_chars = 0


# ============================================================
# 主类
# ============================================================

class LongTermMemoryManager:
    """长期记忆管理器（v2）。

    职责：
    - 维护 memory_store/long_term/ 目录下的 .md 记忆文件
    - 提供按 type 分组渲染的 MEMORY.md 索引
    - 程序化写入 (save_memory) + 按 name 去重更新
    - 按需召回：build_recall_payload 仅装指南+索引；
      async_filter_recommendations 在 strategy 工具执行期间并行触发筛选
    - 信息量驱动的后台抽取：trigger_extraction
    - 会话级 read_file 已读去重
    """

    def __init__(self, base_dir: str = None):
        if base_dir is None:
            base_dir = str(
                Path(__file__).resolve().parent.parent.parent.parent
                / "memory_store" / "long_term"
            )
        self.base_dir = Path(base_dir).resolve()
        self.ensure_dir()

        # 线程安全：保护 _counters / _read_sets
        # 同时也覆盖 MEMORY.md 索引写入，防止 heavy turn 双 agent 并发抽取时条目丢失
        self._lock = threading.Lock()
        self._counters: Dict[str, _ExtractionCounter] = {}
        self._read_sets: Dict[str, Set[str]] = {}

    # ============================================================
    # 目录初始化
    # ============================================================

    def ensure_dir(self) -> None:
        """幂等创建目录及初始 MEMORY.md。"""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        entrypoint = self.base_dir / "MEMORY.md"
        if not entrypoint.exists():
            entrypoint.write_text("", encoding="utf-8")

    # ============================================================
    # 扫描与元数据解析
    # ============================================================

    def scan_memory_files(self) -> List[MemoryHeader]:
        """扫描所有 .md（排除 MEMORY.md），解析 frontmatter，按 mtime 倒序，上限 200。"""
        results: List[MemoryHeader] = []
        try:
            for root, _, files in os.walk(self.base_dir):
                for fname in files:
                    if not fname.endswith(".md") or fname == "MEMORY.md":
                        continue
                    fpath = Path(root) / fname
                    if fpath.is_symlink():
                        logger.warning("忽略符号链接长期记忆文件: %s", fpath.name)
                        continue
                    try:
                        header = self._parse_one_file(fpath, root)
                    except Exception as exc:
                        logger.debug("解析长期记忆文件失败 %s: %s", fpath, exc)
                        continue
                    if header is not None:
                        results.append(header)
        except Exception as exc:
            logger.debug("扫描长期记忆目录失败 %s: %s", self.base_dir, exc)

        results.sort(key=lambda h: h.mtime_ms, reverse=True)
        return results[:MAX_FILES]

    def _parse_one_file(self, fpath: Path, root: str) -> Optional[MemoryHeader]:
        """解析单个 .md 文件，返回 MemoryHeader（解析失败返回 None）。

        兼容旧文件缺 created_at/updated_at 时 fallback 到 mtime 日期。
        缺少或无法解析合法 type 时排除文件，避免污染索引和召回清单。
        """
        st = fpath.stat()
        front_part, body_excerpt = self._read_metadata_sample(fpath)
        meta = self._parse_frontmatter(front_part, fpath)
        if meta is None:
            return None

        raw_type = meta.get("type")
        if raw_type not in MEMORY_TYPES:
            logger.warning("忽略缺失或无效 type 的长期记忆文件: %s", fpath.name)
            return None

        mtime_date = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        rel_path = str(fpath.relative_to(self.base_dir))
        return MemoryHeader(
            filename=rel_path,
            file_path=str(fpath),
            mtime_ms=st.st_mtime * 1000,
            description=self._coerce_description(meta.get("description"), body_excerpt),
            type=raw_type,
            name=self._coerce_name(meta.get("name"), fpath),
            created_at=self._normalize_date(meta.get("created_at")) or mtime_date,
            updated_at=self._normalize_date(meta.get("updated_at")) or mtime_date,
        )

    @staticmethod
    def _read_metadata_sample(fpath: Path) -> tuple[str, str]:
        """读取 frontmatter 与少量正文片段，避免为清单扫描整文件。"""
        front_lines: List[str] = []
        body_lines: List[str] = []
        in_body = False
        front_closed = False

        with open(fpath, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if not in_body:
                    front_lines.append(line)
                    if idx > 0 and line.strip() == "---":
                        in_body = True
                        front_closed = True
                    if len(front_lines) >= FRONTMATTER_MAX_LINES:
                        break
                    continue
                body_lines.append(line)
                if len(body_lines) >= BODY_EXCERPT_MAX_LINES:
                    break

        if not front_closed:
            return "".join(front_lines), ""
        return "".join(front_lines), "".join(body_lines)

    @staticmethod
    def _parse_frontmatter(front_part: str, fpath: Path) -> Optional[Dict[str, Any]]:
        """解析 YAML frontmatter；格式或 YAML 异常时返回 None。"""
        if not front_part.startswith("---\n"):
            logger.warning("忽略缺少 frontmatter 的长期记忆文件: %s", fpath.name)
            return None
        end_idx = front_part.find("\n---\n", 4)
        if end_idx == -1:
            logger.warning("忽略 frontmatter 未闭合的长期记忆文件: %s", fpath.name)
            return None
        try:
            meta = yaml.safe_load(front_part[4:end_idx])
        except yaml.YAMLError as exc:
            logger.warning("忽略 frontmatter 解析失败的长期记忆文件 %s: %s", fpath.name, exc)
            return None
        if not isinstance(meta, dict):
            logger.warning("忽略 frontmatter 非映射的长期记忆文件: %s", fpath.name)
            return None
        return meta

    @staticmethod
    def _coerce_name(value: Any, fpath: Path) -> str:
        """name 缺失时回退到文件 stem。"""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fpath.stem

    def _coerce_description(self, value: Any, body_excerpt: str) -> str:
        """description 缺失时从正文首个有效片段派生短描述。"""
        if isinstance(value, str) and value.strip():
            return self._shorten_description(value)
        return self._derive_description(body_excerpt)

    def _derive_description(self, text: str) -> str:
        """从文本中提取首个有意义行作为短描述。"""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line in ("---",):
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
            if line:
                return self._shorten_description(line)
        return "（无描述）"

    @staticmethod
    def _shorten_description(text: str) -> str:
        """将描述裁剪到安全短长度，避免 manifest 膨胀。"""
        clean = " ".join(str(text).split())
        if len(clean) <= MEMORY_DESCRIPTION_MAX_CHARS:
            return clean
        return clean[:MEMORY_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"

    @staticmethod
    def _normalize_date(value: Any) -> Optional[str]:
        """把 frontmatter 中的日期字段统一为 YYYY-MM-DD 字符串。"""
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        # yaml 可能解析为 datetime.date / datetime.datetime
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return None

    # ============================================================
    # 索引渲染（平铺索引）
    # ============================================================

    def get_entrypoint_text(self) -> str:
        """渲染 MEMORY.md 索引：仅输出平铺索引行，空记忆返回空串。

        project 行带日期与自动 STALE 标记。渲染完成后按字节数与行数做硬截断，
        保证 system prompt 注入大小有上界。
        """
        headers = self.scan_memory_files()
        if not headers:
            return ""

        lines = [self._render_index_line(h) for h in headers]
        raw = "\n".join(lines).rstrip() + "\n"
        return self._truncate_entrypoint_text(raw)

    def _truncate_entrypoint_text(self, raw: str) -> str:
        """按字节与行数限制截断索引文本。"""
        raw_bytes = raw.encode("utf-8")
        if len(raw_bytes) > MAX_ENTRYPOINT_BYTES:
            truncated = raw_bytes[:MAX_ENTRYPOINT_BYTES].decode("utf-8", errors="replace")
            raw = (
                truncated.rsplit("\n", 1)[0]
                + "\n\n（索引过长，已按字节截断。请用工具验证当前事实。）\n"
            )

        split = raw.splitlines()
        if len(split) > MAX_ENTRYPOINT_LINES:
            raw = (
                "\n".join(split[:MAX_ENTRYPOINT_LINES])
                + "\n\n（索引过长，已按行数截断。请用工具验证当前事实。）\n"
            )
        return raw

    def _render_index_line(self, h: MemoryHeader) -> str:
        """单条索引行：- [type] filename — description（project 追加日期与 STALE）。"""
        desc = h.description or "（无描述）"
        base = f"- [{h.type}] {h.filename} — {desc}"
        if h.type == "project":
            date_str = h.updated_at or h.created_at
            if date_str:
                base += f" — {date_str}{self._stale_marker(date_str)}"
        return base

    @staticmethod
    def _stale_marker(date_str: Optional[str]) -> str:
        """若日期距今 >= STALE_THRESHOLD_DAYS 则返回 ' [STALE]'，否则空串。"""
        if not date_str:
            return ""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            delta = (datetime.now().date() - d).days
            return " [STALE]" if delta >= STALE_THRESHOLD_DAYS else ""
        except Exception:
            return ""

    # ============================================================
    # 写入（程序化）
    # ============================================================

    def save_memory(
        self, memory_type: str, name: str, description: str, content: str,
        _skip_index_rebuild: bool = False,
    ) -> Optional[Path]:
        """程序化写入一条记忆。

        - type=feedback/project 时正文必须含 **Why:** 与 **How to apply:**，否则拒绝
        - 以 name 为主键自然去重；已存在则保留 created_at、刷新 updated_at + 内容覆盖
        - 写入后调用 _fire_invalidation 让 section 缓存失效
        - `_skip_index_rebuild`（内部用）：批量写入时跳过单条索引重建，由调用方统一收尾
        """
        if memory_type not in MEMORY_TYPES:
            logger.warning("拒绝写入未知 type: %s (name=%s)", memory_type, name)
            return None
        if not name or not isinstance(name, str):
            logger.warning("拒绝写入：name 必须为非空字符串")
            return None
        if memory_type in ("feedback", "project"):
            if "**Why:**" not in content or "**How to apply:**" not in content:
                logger.warning(
                    "拒绝 %s 类记忆 %s：正文缺少 **Why:** 或 **How to apply:** 段",
                    memory_type, name,
                )
                return None

        safe_name = self._sanitize_name(name)
        target = self.base_dir / f"{safe_name}.md"
        today = datetime.now().strftime("%Y-%m-%d")

        # 已存在则保留 created_at
        created_at = today
        if target.exists():
            try:
                existing = self._parse_one_file(target, str(self.base_dir))
                if existing and existing.created_at:
                    created_at = existing.created_at
            except Exception:
                pass

        try:
            content_text = self._render_memory_file(
                name=safe_name,
                description=description or "",
                memory_type=memory_type,
                created_at=created_at,
                updated_at=today,
                body=content,
            )
            target.write_text(content_text, encoding="utf-8")
        except Exception as exc:
            logger.warning("写入记忆文件失败 %s: %s", target, exc)
            return None

        # 加锁重建索引，防止 heavy turn 双 agent 并发写入时条目丢失
        if not _skip_index_rebuild:
            try:
                with self._lock:
                    self._rebuild_index()
            except Exception as exc:
                logger.warning("重建 MEMORY.md 索引失败: %s", exc)

        _fire_invalidation("memory_write")
        return target

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """把 name 净化为安全的文件名（仅保留字母数字下划线短横线）。"""
        cleaned = "".join(
            c if (c.isalnum() or c in "-_.") else "-" for c in name.strip().lower()
        )
        cleaned = cleaned.strip("-")
        if cleaned:
            return cleaned
        return f"memory-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _render_memory_file(
        name: str, description: str, memory_type: str,
        created_at: str, updated_at: str, body: str,
    ) -> str:
        """生成完整的 .md 文件文本（frontmatter + body）。"""
        # 用 yaml.safe_dump 兜底转义特殊字符
        meta = {
            "name": name,
            "description": description,
            "type": memory_type,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        front = yaml.safe_dump(
            meta, allow_unicode=True, sort_keys=False, default_flow_style=False
        ).rstrip()
        return f"---\n{front}\n---\n\n{body.rstrip()}\n"

    def _rebuild_index(self) -> None:
        """根据当前所有记忆文件重建 MEMORY.md（覆盖式）。"""
        text = self.get_entrypoint_text()
        # get_entrypoint_text 已返回完整索引文本
        (self.base_dir / "MEMORY.md").write_text(text, encoding="utf-8")

    # ============================================================
    # 召回（按需模式）
    # ============================================================

    def build_recall_payload(
        self, query: str = "", session_id: str = "default",
        recent_tools: Optional[List[str]] = None,
    ) -> str:
        """对外主入口：每轮 system prompt 只装载行为指南 + MEMORY.md 索引。

        召回筛选不再在此处启动；strategy 会在主模型返回 tool_calls 后并行触发
        `async_filter_recommendations` 并在工具结果之后追加临时 system meta 推荐。

        兼容签名：保留 query/recent_tools 参数但忽略，便于上层调用点不变。
        """
        _ = (query, session_id, recent_tools)  # 显式忽略
        guide = self._build_guide()
        index_text = self._build_index_section()
        return f"{guide}\n\n## 记忆索引\n\n{index_text}"

    def _build_index_section(self) -> str:
        """构建 system prompt 的索引段，空记忆显示占位文本。"""
        index_text = self.get_entrypoint_text().strip()
        return index_text or "（当前未沉淀任何记忆。）"

    def build_recommendation_section(self, headers: List[MemoryHeader]) -> str:
        """渲染临时推荐 system meta 内容。

        - headers 为空时返回空串，调用方据此决定是否 append；
        - 非空时注入元数据与完整记忆文件内容，并对单条和总量做硬截断。
        """
        if not headers:
            return ""

        lines = ["## 本轮可能相关的长期记忆（已注入全文，仅供参考）", ""]
        remaining = RECALL_MAX_TOTAL_CHARS
        for h in headers:
            block = self._render_recommendation_memory_block(h)
            chunk, remaining = self._consume_recall_budget(block, remaining)
            if not chunk:
                lines.append(self._omitted_memory_note(h, len(block)))
                continue
            lines.append(chunk)
            lines.append("")

        return "\n".join(lines).rstrip()

    def _render_recommendation_memory_block(self, h: MemoryHeader) -> str:
        """生成单条推荐记忆块，包含元数据与受限全文。"""
        desc = h.description or "（无描述）"
        name = h.name or Path(h.filename).stem
        content = self.get_memory_content(h.filename)
        content = self._truncate_text(content, RECALL_MAX_CHARS_PER_MEMORY)
        if not content:
            content = "（记忆文件内容为空或不可读取。）"
        return (
            f"### [{h.type}] {h.filename} ({name})\n"
            f"- 描述：{desc}\n"
            f"- created_at：{h.created_at or '未知'}\n"
            f"- updated_at：{h.updated_at or '未知'}\n\n"
            "```markdown\n"
            f"{content.rstrip()}\n"
            "```"
        )

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """把文本截断到指定字符数，并显式标注省略字符数。"""
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        marker = f"\n\n（内容过长，已省略 {omitted} 个字符。）"
        if limit <= len(marker):
            return marker[-limit:]
        return text[:limit - len(marker)] + marker

    def _consume_recall_budget(self, block: str, remaining: int) -> tuple[str, int]:
        """消费总召回预算，预算耗尽时返回空块。"""
        if remaining <= 0:
            return "", 0
        if len(block) <= remaining:
            return block, remaining - len(block)
        return self._truncate_text(block, remaining), 0

    @staticmethod
    def _omitted_memory_note(h: MemoryHeader, omitted_chars: int) -> str:
        """生成总预算耗尽后的省略提示。"""
        return f"（召回总预算已耗尽，已省略 {h.filename} 的 {omitted_chars} 个字符。）"

    # 兼容旧测试/调用点的私有别名
    _build_recommendation_section = build_recommendation_section

    async def async_filter_recommendations(
        self,
        query: str,
        session_id: str = "default",
        recent_tools: Optional[List[str]] = None,
    ) -> List[MemoryHeader]:
        """异步包装 find_relevant：在 strategy 与工具执行并行时调用。

        筛选失败必须返回 []，绝不能影响工具执行链路。
        """
        try:
            return await asyncio.to_thread(
                self.find_relevant,
                query,
                session_id,
                recent_tools or [],
            )
        except Exception as exc:
            logger.debug("长期记忆异步筛选失败: %s", exc)
            return []

    def find_relevant(
        self, query: str, session_id: str = "default",
        recent_tools: Optional[List[str]] = None,
    ) -> List[MemoryHeader]:
        """返回 ≤5 个推荐 MemoryHeader（过滤已读 + 小模型筛选）。

        失败/小模型未配置/降级时返回 []，不强凑。
        """
        headers = self.scan_memory_files()
        if not headers:
            return []

        read_set = self.get_read_set(session_id)
        candidates = [h for h in headers if h.filename not in read_set]
        if not candidates:
            return []

        manifest = self.format_manifest(candidates)
        selected_filenames = self._call_select_llm(query, manifest, recent_tools or [])
        if not selected_filenames:
            return []

        by_filename = {h.filename: h for h in candidates}
        return [
            by_filename[f] for f in selected_filenames if f in by_filename
        ][:RECALL_TOP_K]

    def format_manifest(self, headers: List[MemoryHeader]) -> str:
        """将候选清单格式化为可读 manifest（小模型输入）。"""
        if not headers:
            return "（无记忆文件）"
        lines = []
        for h in headers:
            desc = f" — {h.description}" if h.description else ""
            lines.append(f"- [{h.type}] {h.filename}{desc}")
        return "\n".join(lines)

    def _call_select_llm(
        self, query: str, manifest: str, recent_tools: List[str]
    ) -> List[str]:
        """调用小模型执行召回筛选。小模型未配置或降级到主模型时直接跳过。"""
        try:
            config = get_small_config()
            if not config or not config.get("api_key"):
                return []
            if self._is_small_degraded_to_main(config):
                logger.debug("小模型降级到主模型，跳过长期记忆召回以节约资源")
                return []

            tools_section = ""
            if recent_tools:
                tools_section = f"\n\n最近使用的工具：{', '.join(recent_tools)}"

            from openai import OpenAI
            client = OpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                max_retries=1,
                timeout=15,
            )
            try:
                response = client.chat.completions.create(
                    model=config["model_name"],
                    messages=[
                        {"role": "system", "content": _SELECT_PROMPT_CN},
                        {
                            "role": "user",
                            "content": (
                                f"查询：{query}\n\n候选记忆清单：\n{manifest}"
                                f"{tools_section}"
                            ),
                        },
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content.strip()
                result = json.loads(content)
                selected = result.get("selected", [])
                if not isinstance(selected, list):
                    logger.warning(
                        "小模型返回的 selected 字段类型异常：%s",
                        type(selected).__name__,
                    )
                    return []
                return [s for s in selected if isinstance(s, str)]
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("长期记忆召回失败: %s", exc)
            return []

    @staticmethod
    def _is_small_degraded_to_main(small_config: Dict[str, Any]) -> bool:
        """三参数完全等同主模型即视为降级（节约主模型配额）。"""
        try:
            main_config = get_main_config()
            return (
                small_config.get("api_key") == main_config.get("api_key")
                and small_config.get("base_url") == main_config.get("base_url")
                and small_config.get("model_name") == main_config.get("model_name")
            )
        except Exception:
            return False

    # ============================================================
    # 行为指南（中文）
    # ============================================================

    def _build_guide(self) -> str:
        """构建行为指南部分（精简版，索引和推荐另外渲染）。"""
        return (
            "# 长期记忆系统\n\n"
            f"你拥有一个基于文件的持久化记忆系统，路径在 `{str(self.base_dir)}`。\n\n"
            "记忆由后台抽取 agent 在每轮对话结束后自动维护，**主模型无需手动写入**。\n"
            "你的职责：不要为了加载长期记忆而主动调用 `read_file`；如果本轮进入工具调用分支，"
            "系统会在工具结果之后自动筛选并注入相关记忆全文。\n\n"
            "## 记忆分类\n\n"
            "共 4 类记忆，存储分类但召回时统一筛选：\n\n"
            "- **用户画像 (user)**：用户身份、职业、长期目标。积累型，长期稳定，无陈旧度警告。\n"
            "- **偏好反馈 (feedback)**：用户对协作方式的稳定偏好或明确禁忌。"
            "积累型，无陈旧度警告；正文必含 **Why:** 和 **How to apply:**。\n"
            "- **项目动态 (project)**：项目目标、阶段决定、外部约束、截止日期。"
            "**易过期**，>2 天会显示 `[STALE]` 标记；正文必含 **Why:** 和 **How to apply:**。\n"
            "- **外部指针 (reference)**：外部文档链接、知识源、工具入口。积累型，无陈旧度警告。\n\n"
            "## 如何使用\n\n"
            "1. 下方「记忆索引」平铺列出所有记忆，每条仅显示类型、文件名和一行描述。\n"
            "2. 不要仅为了加载长期记忆正文而调用 `read_file`。当主模型返回 tool_calls 后，"
            "系统会在工具结果之后追加一段「本轮可能相关的长期记忆」，其中已包含筛选出的记忆全文。\n"
            "3. 工具只用于验证当前事实，例如确认文件是否存在、函数/参数是否仍存在、外部状态是否变化。\n"
            "4. **看到 `[STALE]` 标记的项目动态条目**，请把它当作历史快照而非当前事实——"
            "若需要基于它提出建议，请先用工具校验当前状态。\n"
            "5. 用户画像、偏好反馈、外部指针无 STALE 标记，可信度较高，"
            "但涉及当前代码、文件或外部状态时仍以工具观察为准。\n\n"
            "## 引用前的校验\n\n"
            "- 不要调用 `read_file` 只为读取长期记忆正文；等待系统在工具分支后注入相关全文\n"
            "- 记忆指向某文件路径：确认文件存在\n"
            "- 记忆指向某函数或参数：grep 一下\n"
            "- 用户即将基于你的建议行动：先校验再说\n\n"
            "「记忆里说 X 存在」不等于「X 现在还存在」。\n"
            "若记忆与当前观察矛盾，相信你当下看到的事实。"
        )

    # ============================================================
    # 已读 set 维护
    # ============================================================

    def mark_read(self, filename: str, session_id: str) -> None:
        """记录该 session 已通过 read_file 实读过的记忆文件名。"""
        if not filename:
            return
        with self._lock:
            self._read_sets.setdefault(session_id, set()).add(filename)

    def get_read_set(self, session_id: str) -> Set[str]:
        with self._lock:
            return set(self._read_sets.get(session_id, set()))

    def clear_read_set(self, session_id: str) -> None:
        with self._lock:
            self._read_sets.pop(session_id, None)
            self._counters.pop(session_id, None)

    def reset_all_session_state(self) -> None:
        """清空所有 session 的内存态（read_sets / counters）。

        用于 clear_context 等粗粒度失效场景——回调拿不到 session_id 时，最稳的做法
        就是整体重置，以免下一轮读到旧 session 的脏缓存。
        """
        with self._lock:
            self._read_sets.clear()
            self._counters.clear()

    # ============================================================
    # 后台抽取（信息量累积触发）
    # ============================================================

    def trigger_extraction(self, messages: List[Dict], session_id: str) -> None:
        """每个 turn 完成后由 strategy 调用。

        信号量任一阈值达成即 spawn 抽取线程；heavy turn 启动 2 个 agent 分批。
        """
        try:
            new_user, new_tool, new_chars = self._compute_increment(messages, session_id)
        except Exception as exc:
            logger.warning("[ltm] 计算抽取增量失败 (session=%s): %s", session_id, exc)
            return

        should_trigger = False
        is_heavy = False
        with self._lock:
            counter = self._counters.setdefault(session_id, _ExtractionCounter())
            counter.pending_user_turns += new_user
            counter.pending_tool_calls += new_tool
            counter.pending_chars += new_chars
            counter.last_message_count = len(messages)

            should_trigger = (
                counter.pending_user_turns >= EXTRACT_TRIGGER_USER_TURNS
                or counter.pending_tool_calls >= EXTRACT_TRIGGER_TOOL_CALLS
                or counter.pending_chars >= EXTRACT_TRIGGER_CHARS
            )
            is_heavy = counter.pending_tool_calls >= HEAVY_TURN_TOOL_THRESHOLD
            if should_trigger:
                counter.reset()
                counter.last_extract_at = time.time()

        if not should_trigger:
            return

        # 深拷贝隔离主对话引用
        copied = copy.deepcopy(messages)
        if is_heavy and len(copied) >= 4:
            mid = len(copied) // 2
            self._spawn_extraction_thread(copied[:mid + 1], session_id, label="A")
            self._spawn_extraction_thread(copied[mid:], session_id, label="B")
        else:
            self._spawn_extraction_thread(copied, session_id, label="")

    def _compute_increment(
        self, messages: List[Dict], session_id: str
    ) -> tuple[int, int, int]:
        """根据上次记录的位置统计本轮新增（user 轮数 / tool_calls 数 / 字符数）。"""
        msgs_len = len(messages)
        with self._lock:
            counter = self._counters.get(session_id)
            start = counter.last_message_count if counter else 0
        # 防御性 clamp：messages 可能在锁外被截断 / 越界
        start = max(0, min(start, msgs_len))

        new_slice = messages[start:]
        new_user = sum(1 for m in new_slice if m.get("role") == "user")
        new_tool = 0
        new_chars = 0
        for m in new_slice:
            role = m.get("role")
            if role == "assistant":
                content = m.get("content") or ""
                if isinstance(content, str):
                    new_chars += len(content)
                tcs = m.get("tool_calls") or []
                if isinstance(tcs, list):
                    new_tool += len(tcs)
            elif role == "tool":
                content = m.get("content") or ""
                if isinstance(content, str):
                    new_chars += len(content)
        return new_user, new_tool, new_chars

    def _spawn_extraction_thread(
        self, messages_copy: List[Dict], session_id: str, label: str = ""
    ) -> None:
        t = threading.Thread(
            target=self._run_extraction_agent,
            args=(messages_copy, session_id, label),
            daemon=True,
            name=f"ltm-extract-{session_id}{label}",
        )
        t.start()

    def _run_extraction_agent(
        self, messages: List[Dict], session_id: str, label: str = ""
    ) -> None:
        """后台抽取 agent：调小模型解析 JSON 并落地记忆。"""
        try:
            config = get_small_config()
            if not config or not config.get("api_key"):
                return
            if self._is_small_degraded_to_main(config):
                logger.debug("小模型降级，跳过后台抽取 (session=%s, label=%s)", session_id, label)
                return

            # 清洗 messages，去掉非法字段，保留 role/content/tool_calls/name/tool_call_id
            cleaned = self._sanitize_messages_for_extraction(messages)
            extraction_msg = {
                "role": "user",
                "content": _EXTRACTION_PROMPT_CN + "\n\n请只输出 JSON。",
            }
            payload = cleaned + [extraction_msg]

            from openai import OpenAI
            client = OpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                max_retries=1,
                timeout=30,
            )
            try:
                response = client.chat.completions.create(
                    model=config["model_name"],
                    messages=payload,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content.strip()
            finally:
                try:
                    client.close()
                except Exception:
                    pass

            self._persist_extracted_memories(content, session_id, label)
        except Exception as exc:
            logger.warning(
                "抽取 agent 失败 (session=%s, label=%s): %s", session_id, label, exc
            )

    @staticmethod
    def _sanitize_messages_for_extraction(messages: List[Dict]) -> List[Dict]:
        """剥离 messages 中的 OpenAI 不识别字段，便于直接喂给抽取 agent。

        只保留：role/content/name/tool_calls/tool_call_id。
        跳过 is_meta 标记的临时推荐消息，防止其被抽取 agent 再沉淀为长期记忆。
        净化后若缺 role 则记 debug log 并跳过（不再静默丢弃，便于排查链路异常）。
        """
        allowed = {"role", "content", "name", "tool_calls", "tool_call_id"}
        result = []
        for m in messages:
            if m.get("is_meta"):
                continue
            clean = {k: v for k, v in m.items() if k in allowed}
            if "role" not in clean:
                logger.debug(
                    "抽取净化跳过无 role 的消息: keys=%s", list(m.keys())[:5]
                )
                continue
            result.append(clean)
        return result

    def _persist_extracted_memories(
        self, content: str, session_id: str, label: str
    ) -> None:
        """解析抽取 agent 返回的 JSON 并落地。"""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(
                "抽取 agent 返回非 JSON (session=%s, label=%s)", session_id, label
            )
            return

        memories = data.get("memories")
        if not isinstance(memories, list):
            return

        saved = 0
        for item in memories[:EXTRACT_MAX_MEMORIES_PER_CALL]:
            normalized = self._normalize_extracted_memory_item(item, session_id, label)
            if normalized is None:
                continue
            path = self.save_memory(
                memory_type=normalized["type"],
                name=normalized["name"],
                description=normalized["description"],
                content=normalized["content"],
                _skip_index_rebuild=True,
            )
            if path:
                saved += 1
                logger.info(
                    "[ltm] 后台抽取已落地 %s (session=%s, label=%s)",
                    path.name, session_id, label,
                )
        if saved:
            # 批量写入完成后统一重建索引一次（加锁，与并发抽取互斥）
            try:
                with self._lock:
                    self._rebuild_index()
            except Exception as exc:
                logger.warning("批量持久化后重建 MEMORY.md 索引失败: %s", exc)
            _fire_invalidation("memory_write")
            logger.info(
                "[ltm] 本批抽取共落地 %d 条 (session=%s, label=%s)",
                saved, session_id, label,
            )

    def _normalize_extracted_memory_item(
        self, item: Any, session_id: str, label: str
    ) -> Optional[Dict[str, str]]:
        """校验并补齐抽取 agent 返回的单条记忆。"""
        if not isinstance(item, dict):
            logger.warning(
                "抽取记忆条目类型异常 (session=%s, label=%s): %s",
                session_id, label, type(item).__name__,
            )
            return None

        memory_type = item.get("type")
        if memory_type not in MEMORY_TYPES:
            logger.warning(
                "拒绝抽取记忆：无效 type=%s (session=%s, label=%s)",
                memory_type, session_id, label,
            )
            return None

        content = str(item.get("content") or "").strip()
        if not content:
            logger.warning(
                "拒绝抽取记忆：content 为空 (type=%s, session=%s, label=%s)",
                memory_type, session_id, label,
            )
            return None

        description = str(item.get("description") or "").strip()
        if not description:
            description = self._derive_description(content)

        name = str(item.get("name") or "").strip()
        if not name:
            name = self._sanitize_name(description or content)

        return {
            "type": memory_type,
            "name": name,
            "description": description,
            "content": content,
        }
    # ============================================================
    # CLI 辅助
    # ============================================================

    def list_memory_files(self) -> List[str]:
        """返回所有记忆文件名列表（用于 CLI）。"""
        return [h.filename for h in self.scan_memory_files()]

    def list_memory_headers(self) -> List[MemoryHeader]:
        """返回长期记忆元数据列表（用于 CLI 展示）。"""
        return self.scan_memory_files()

    def get_memory_content(self, path: str) -> str:
        """读取单个记忆文件的完整内容，拒绝越过长期记忆目录。"""
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.base_dir / path
            fpath = target.resolve()
            if not self._is_within_base_dir(fpath):
                return ""
            return fpath.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _is_within_base_dir(self, path: Path) -> bool:
        """判断目标路径是否仍在长期记忆根目录内。"""
        try:
            path.relative_to(self.base_dir)
            return True
        except ValueError:
            return False

    def clear_all(self) -> int:
        """清空所有记忆文件 + 重置 MEMORY.md + 清空全部 session 状态。"""
        count = 0
        for root, _, files in os.walk(self.base_dir):
            for fname in files:
                if not fname.endswith(".md") or fname == "MEMORY.md":
                    continue
                fpath = Path(root) / fname
                try:
                    fpath.unlink()
                    count += 1
                except Exception as exc:
                    logger.warning("删除长期记忆文件失败 %s: %s", fpath, exc)

        try:
            (self.base_dir / "MEMORY.md").write_text("", encoding="utf-8")
        except Exception as exc:
            logger.warning("重置长期记忆索引失败 %s: %s", self.base_dir / "MEMORY.md", exc)

        # 清空所有内存态：counters / read_sets
        with self._lock:
            self._counters.clear()
            self._read_sets.clear()

        _fire_invalidation("memory_clear")
        return count
