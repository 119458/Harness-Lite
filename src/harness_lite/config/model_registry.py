"""模型上下文窗口注册表。

提供统一的模型名 → 上下文窗口大小映射查询，是 max_allowed_tokens 解析的单一事实来源。

匹配策略（resolve_context_window）：
    1. 空字符串 → 默认值
    2. 精确匹配（case-insensitive）
    3. 子串匹配（双向 in 测试，选最长 pattern 作为最佳匹配）
    4. 厂商前缀剥离（含 "/" 时取最后一段递归调用一次）
    5. 都不匹配 → 默认值

数据来源标记：
    ✅ = 已通过官方文档/官方 GitHub README/官方论文核实
    ⚠️  = 来自模型训练知识（截至 2025 年初），未在本次任务中实时核实
         （工具受限：docs.anthropic.com/ai.google.dev/platform.moonshot.ai 等域名被沙箱拦截）
         用户后续可手动核实并修正
"""
from typing import List, Tuple
import logging

logger = logging.getLogger("harness_lite.config")

DEFAULT_CONTEXT_WINDOW = 128_000

# 子串匹配最短 pattern 长度阈值。
# 防止 "o1" / "o3" 等过短 pattern 在子串匹配阶段误命中不相关名称
# （如 "kobold-o1-test"）。精确匹配阶段不受此约束。
MIN_SUBSTRING_PATTERN_LEN = 4

MODEL_CONTEXT_WINDOWS: List[Tuple[str, int]] = [
    # ============ OpenAI（✅ 全部已核实，来源 LiteLLM 注册表 + 官方 specs） ============
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo-preview", 128_000),       # ✅ Turbo 预览别名
    ("gpt-4-turbo-2024-04-09", 128_000),    # ✅ Turbo 日期快照
    ("gpt-4-turbo", 128_000),
    ("gpt-4-1106-preview", 128_000),        # ✅ Turbo 预览（128K，避免误配到 gpt-4=8192）
    ("gpt-4-0125-preview", 128_000),        # ✅ Turbo 预览（128K）
    ("gpt-4-vision-preview", 128_000),      # ✅ Turbo with Vision（128K）
    ("gpt-4-32k", 32_768),
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo-16k", 16_384),
    ("gpt-3.5-turbo", 16_385),
    ("o3-mini", 200_000),
    ("o3", 200_000),
    ("o1-mini", 128_000),
    ("o1-preview", 128_000),                # ✅ o1 预览版
    ("o1-pro", 200_000),
    ("o1", 200_000),

    # ============ Anthropic（⚠️ 待用户核实） ============
    # 来源：训练知识。Claude 3 系列起官方公告均为 200K context window
    ("claude-haiku-4-5", 200_000),
    ("claude-opus-4-7", 200_000),
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4-6", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3-7-sonnet", 200_000),
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-3-opus", 200_000),
    ("claude-3-sonnet", 200_000),
    ("claude-3-haiku", 200_000),

    # ============ DeepSeek（✅ 已核实，来源官方 GitHub README） ============
    ("deepseek-r1", 128_000),         # ✅ DeepSeek-R1 GitHub README
    ("deepseek-v3", 128_000),         # ✅ DeepSeek-V3 GitHub README
    ("deepseek-chat", 128_000),       # ✅ 对应 V3/legacy chat 端点
    ("deepseek-reasoner", 128_000),   # ✅ 对应 R1 端点
    ("deepseek-v4-flash", 128_000),   # ⚠️ 用户当前 .env 实际值；官方未确认，按默认填
    ("deepseek-v4-pro", 128_000),     # ⚠️ 用户场景中的别名

    # ============ 阿里 Qwen（✅ 已核实，来源官方 blog + GitHub） ============
    ("qwen3", 128_000),               # ✅ Qwen3 官方 GitHub（原始 Qwen3 系列 128K，2507 版扩到 256K/1M）
    ("qwen2.5", 128_000),             # ✅ 官方 blog "context length of 128K (131,072) tokens"
    ("qwen2", 128_000),
    ("qwen-max", 32_768),             # ⚠️ 阿里云 DashScope 商用 API
    ("qwen-plus", 128_000),           # ⚠️
    ("qwen-turbo", 1_000_000),        # ⚠️ 长上下文版

    # ============ Meta Llama（✅ 已核实，来源 Meta 官方 model card） ============
    ("llama-3.3", 128_000),           # ⚠️ 沿袭 3.1 架构
    ("llama3.3", 128_000),
    ("llama-3.1", 128_000),           # ✅ Meta llama-models GitHub MODEL_CARD.md
    ("llama3.1", 128_000),
    ("llama-3", 8_192),               # ✅ Meta llama3 GitHub MODEL_CARD.md
    ("llama3", 8_192),

    # ============ Moonshot / Kimi（⚠️ 待用户核实） ============
    ("kimi-k2", 128_000),             # ⚠️ 训练知识；K2 是 2025/07 开源 1T MoE
    ("moonshot-v1-8k", 8_192),        # ⚠️ 三档 API 商用版本
    ("moonshot-v1-32k", 32_768),
    ("moonshot-v1-128k", 128_000),
    ("moonshot-v1", 128_000),         # 通用兜底

    # ============ Mistral（✅ 已核实） ============
    ("mistral-large", 128_000),       # ✅ OpenRouter（mistral-large-2407/2411 = 128K）
    ("mistral-medium", 32_000),       # ⚠️
    ("mistral-small", 32_000),        # ✅ 24B-Instruct-2501 = 32K（3.1 起 128K 待用户区分）
    ("mixtral", 32_768),              # ✅ Mixtral of Experts arXiv:2401.04088
    ("codestral", 32_000),            # ⚠️

    # ============ Google Gemini（⚠️ 待用户核实） ============
    # 训练知识：Gemini 1.5 起均为长上下文家族
    ("gemini-2.5-pro", 1_048_576),    # ⚠️ ~1M
    ("gemini-2.5-flash", 1_048_576),  # ⚠️
    ("gemini-2.0-flash", 1_048_576),  # ⚠️
    ("gemini-2.0", 1_048_576),
    ("gemini-1.5-pro", 2_097_152),    # ⚠️ ~2M（1.5 Pro 是已知最大窗口）
    ("gemini-1.5-flash", 1_048_576),  # ⚠️
    ("gemini-1.5", 1_048_576),
    ("gemini-1.0", 32_000),           # ⚠️

    # ============ 智谱 GLM（⚠️ 待用户核实） ============
    ("glm-4.6", 200_000),             # ⚠️ GLM-4.6 官方公告 200K
    ("glm-4.5", 128_000),             # ⚠️
    ("glm-4-plus", 128_000),          # ⚠️
    ("glm-4-long", 1_000_000),        # ⚠️ 长上下文专用
    ("glm-4-flash", 128_000),         # ⚠️
    ("glm-4", 128_000),               # ⚠️ 通用
    ("glm-3-turbo", 128_000),         # ⚠️

    # ============ 百度文心（⚠️ 待用户核实） ============
    ("ernie-4.5", 128_000),           # ⚠️
    ("ernie-4.0-turbo", 128_000),     # ⚠️
    ("ernie-4.0", 8_192),             # ⚠️ 早期版本
    ("ernie-3.5", 8_192),             # ⚠️
    ("ernie-speed", 128_000),         # ⚠️
    ("ernie-lite", 8_192),            # ⚠️

    # ============ 字节豆包（⚠️ 待用户核实） ============
    ("doubao-1.5-pro-256k", 256_000), # ⚠️
    ("doubao-1.5-pro", 32_000),       # ⚠️
    ("doubao-pro-256k", 256_000),     # ⚠️
    ("doubao-pro-128k", 128_000),     # ⚠️
    ("doubao-pro-32k", 32_000),       # ⚠️
    ("doubao-pro", 32_000),           # 通用兜底
    ("doubao-lite-128k", 128_000),    # ⚠️
    ("doubao-lite-32k", 32_000),      # ⚠️
    ("doubao-lite", 32_000),

    # ============ 腾讯混元（⚠️ 待用户核实） ============
    ("hunyuan-turbo", 128_000),       # ⚠️
    ("hunyuan-pro", 32_000),          # ⚠️
    ("hunyuan-large", 256_000),       # ⚠️
    ("hunyuan-standard-256k", 256_000),
    ("hunyuan-standard", 32_000),     # ⚠️
    ("hunyuan-lite", 256_000),        # ⚠️

    # ============ 零一万物 Yi（⚠️ 待用户核实） ============
    ("yi-lightning", 16_000),         # ⚠️
    ("yi-large-turbo", 32_000),       # ⚠️
    ("yi-large", 32_000),             # ⚠️
    ("yi-medium-200k", 200_000),
    ("yi-medium", 16_000),
    ("yi-spark", 16_000),

    # ============ 阶跃 Step（⚠️ 待用户核实） ============
    ("step-2-16k", 16_000),
    ("step-1-256k", 256_000),
    ("step-1-128k", 128_000),
    ("step-1-32k", 32_000),
    ("step-1-8k", 8_000),

    # ============ MiniMax（⚠️ 待用户核实） ============
    ("abab6.5s", 245_760),            # ⚠️ ABAB 6.5s 官宣 245K
    ("abab6.5", 8_192),               # ⚠️
    ("minimax-text-01", 1_000_000),   # ⚠️ 4M token 总上下文，分块 1M

    # ============ 商汤 SenseChat（⚠️ 待用户核实） ============
    ("sensechat-32k", 32_000),
    ("sensechat-128k", 128_000),
    ("sensechat", 32_000),
]


def resolve_context_window(model_name: str) -> int:
    """解析模型上下文窗口大小。

    策略：精确匹配 → 子串匹配（选最长 pattern 作为最佳匹配）
        → 厂商前缀剥离重试一次 → 默认值。

    例：
        'moonshotai/kimi-k2.6'        → 剥离前缀 → 子串匹配 'kimi-k2' → 128000
        'deepseek-ai/deepseek-v4-flash' → 剥离前缀 → 精确匹配 'deepseek-v4-flash' → 128000
        'gpt-4o-2024-05-13'           → 子串匹配 'gpt-4o' → 128000
        'auto'                         → 无匹配 → 128000 (默认)
        ''                             → 默认

    Args:
        model_name: 模型名称（可包含厂商前缀如 "deepseek-ai/deepseek-r1"）

    Returns:
        上下文窗口 token 数；无法识别时返回 DEFAULT_CONTEXT_WINDOW。
    """
    if not model_name or not model_name.strip():
        logger.info("model_name 为空，返回默认上下文窗口 %d", DEFAULT_CONTEXT_WINDOW)
        return DEFAULT_CONTEXT_WINDOW

    name = model_name.strip().lower()

    # 1. 精确匹配
    for pattern, window in MODEL_CONTEXT_WINDOWS:
        if pattern.lower() == name:
            return window

    # 2. 子串匹配：双向 in 测试，选最长 pattern。过短 pattern 跳过避免误命中。
    best_pattern = ""
    best_window = 0
    for pattern, window in MODEL_CONTEXT_WINDOWS:
        p_lower = pattern.lower()
        if len(p_lower) < MIN_SUBSTRING_PATTERN_LEN:
            continue
        if p_lower in name or name in p_lower:
            if len(p_lower) > len(best_pattern):
                best_pattern = p_lower
                best_window = window
    if best_pattern:
        return best_window

    # 3. 厂商前缀剥离：含 "/" 时取最后一段递归调用一次
    if "/" in name:
        suffix = name.rsplit("/", 1)[-1]
        if suffix and suffix != name:
            return resolve_context_window(suffix)

    # 4. 兜底
    logger.info(
        "未在注册表识别到模型 '%s'，返回默认上下文窗口 %d",
        model_name, DEFAULT_CONTEXT_WINDOW,
    )
    return DEFAULT_CONTEXT_WINDOW
