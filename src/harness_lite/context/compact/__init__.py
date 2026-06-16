"""分层上下文管理管线（5 层渐进减负）。"""
from harness_lite.context.compact.types import (
    MessageMeta,
    ToolResultRef,
    CompactionResult,
    LayerStats,
    TokenCounter,
)
from harness_lite.context.compact.storage import (
    LargeResultStore,
    DiskOffloadLayer,
    LARGE_RESULT_THRESHOLD_BYTES,
    LARGE_RESULT_PREVIEW_CHARS,
)
from harness_lite.context.compact.local_layers import (
    SnipLayer,
    TimeDecayLayer,
    COMPACTABLE_TOOLS,
    KEEP_RECENT_TOOL_RESULTS,
    GAP_THRESHOLD_MINUTES,
    TIME_DECAY_PROACTIVE_RATIO,
)
from harness_lite.context.compact.prompts import (
    AUTO_COMPACT_PROMPT_ZH,
    parse_summary_block,
)
from harness_lite.context.compact.auto_compact import (
    AutoCompactLayer,
    AUTOCOMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
)
from harness_lite.context.compact.pipeline import (
    CompactPipeline,
    ContextCollapse,
    find_safe_cut_points,
    nearest_safe_cut,
    META_ID_KEY,
)

__all__ = [
    "MessageMeta",
    "ToolResultRef",
    "CompactionResult",
    "LayerStats",
    "TokenCounter",
    "LargeResultStore",
    "DiskOffloadLayer",
    "LARGE_RESULT_THRESHOLD_BYTES",
    "LARGE_RESULT_PREVIEW_CHARS",
    "SnipLayer",
    "TimeDecayLayer",
    "COMPACTABLE_TOOLS",
    "KEEP_RECENT_TOOL_RESULTS",
    "GAP_THRESHOLD_MINUTES",
    "TIME_DECAY_PROACTIVE_RATIO",
    "AUTO_COMPACT_PROMPT_ZH",
    "parse_summary_block",
    "AutoCompactLayer",
    "AUTOCOMPACT_BUFFER_TOKENS",
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES",
    "CompactPipeline",
    "ContextCollapse",
    "find_safe_cut_points",
    "nearest_safe_cut",
    "META_ID_KEY",
]
