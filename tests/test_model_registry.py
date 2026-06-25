"""模型上下文窗口注册表单元测试。

覆盖 resolve_context_window 的全部匹配路径：
1. 精确匹配（不被更长的 pattern 子串误伤）
2. 子串匹配（选最长 pattern）
3. 大小写不敏感
4. 厂商前缀剥离（递归一次）
5. 更具体的 pattern 优先（最长匹配）
6. 国内厂商（GLM/豆包/混元）
7. Gemini 长上下文家族
8. 未知模型回退默认值
9. 空字符串 / 纯空白回退默认值
10. 厂商前缀但后缀也未知（避免无限递归）
"""
from __future__ import annotations

from harness_lite.config.model_registry import (
    DEFAULT_CONTEXT_WINDOW,
    resolve_context_window,
)


# ============================================================
# 1. 精确匹配
# ============================================================
def test_exact_match_gpt_4o():
    assert resolve_context_window("gpt-4o") == 128_000


def test_exact_match_gpt_4_not_misidentified_as_turbo():
    """gpt-4 应当精确命中 8192，不被 gpt-4-turbo (128k) 顶替。"""
    assert resolve_context_window("gpt-4") == 8_192


def test_exact_match_gpt_4_32k():
    assert resolve_context_window("gpt-4-32k") == 32_768


# ============================================================
# 2. 子串匹配
# ============================================================
def test_substring_match_gpt_4o_dated():
    assert resolve_context_window("gpt-4o-2024-05-13") == 128_000


def test_substring_match_gpt_4_1106_preview():
    """gpt-4-1106-preview 是 GPT-4 Turbo 预览版（128K），
    必须通过精确条目命中，不能退化到 gpt-4=8192。
    """
    assert resolve_context_window("gpt-4-1106-preview") == 128_000
    assert resolve_context_window("gpt-4-0125-preview") == 128_000
    assert resolve_context_window("gpt-4-vision-preview") == 128_000


# ============================================================
# 3. 大小写不敏感
# ============================================================
def test_case_insensitive_uppercase_gpt_4o():
    assert resolve_context_window("GPT-4O") == 128_000


def test_case_insensitive_mixed_case_claude():
    assert resolve_context_window("Claude-3-5-Sonnet") == 200_000


# ============================================================
# 4. 厂商前缀剥离
# ============================================================
def test_vendor_prefix_moonshot_kimi():
    """moonshotai/kimi-k2.6 → 子串匹配 kimi-k2 → 128000。"""
    assert resolve_context_window("moonshotai/kimi-k2.6") == 128_000


def test_vendor_prefix_deepseek_v4_flash():
    """deepseek-ai/deepseek-v4-flash → 子串匹配 deepseek-v4-flash → 128000。"""
    assert resolve_context_window("deepseek-ai/deepseek-v4-flash") == 128_000


# ============================================================
# 5. 更具体的 pattern 优先（最长匹配）
# ============================================================
def test_longer_pattern_wins_gpt_4o_mini():
    """gpt-4o-mini 应命中精确，不应被 gpt-4o (128k) 顶替（两者同值，但精确优先于子串）。"""
    assert resolve_context_window("gpt-4o-mini") == 128_000


def test_longer_pattern_wins_o1_mini_over_o1():
    """o1-mini 应精确命中 128000，不被 o1 (200000) 顶替。"""
    assert resolve_context_window("o1-mini") == 128_000


# ============================================================
# 6. 国内厂商
# ============================================================
def test_domestic_glm_4_6():
    assert resolve_context_window("glm-4.6") == 200_000


def test_domestic_doubao_pro_256k():
    assert resolve_context_window("doubao-pro-256k") == 256_000


def test_domestic_hunyuan_large():
    assert resolve_context_window("hunyuan-large") == 256_000


# ============================================================
# 7. Gemini 长上下文
# ============================================================
def test_gemini_2_5_pro():
    assert resolve_context_window("gemini-2.5-pro") == 1_048_576


def test_gemini_1_5_pro():
    assert resolve_context_window("gemini-1.5-pro") == 2_097_152


# ============================================================
# 8. 未知模型回退默认值
# ============================================================
def test_unknown_model_auto():
    assert resolve_context_window("auto") == DEFAULT_CONTEXT_WINDOW


def test_unknown_model_random_name():
    assert resolve_context_window("some-random-name") == DEFAULT_CONTEXT_WINDOW


# ============================================================
# 9. 空字符串 / 纯空白
# ============================================================
def test_empty_string_returns_default():
    assert resolve_context_window("") == DEFAULT_CONTEXT_WINDOW


def test_whitespace_only_returns_default():
    assert resolve_context_window("   ") == DEFAULT_CONTEXT_WINDOW


# ============================================================
# 10. 厂商前缀但后缀未知（不应无限递归崩溃）
# ============================================================
def test_vendor_prefix_unknown_suffix_returns_default():
    """org/unknown-model：剥离前缀后仍无匹配，应安全返回默认值，不崩溃。"""
    assert resolve_context_window("org/unknown-model") == DEFAULT_CONTEXT_WINDOW


# ============================================================
# 11. 短 pattern 防误命中（o1/o3 等 2 字符 pattern 不参与子串匹配）
# ============================================================
def test_short_pattern_no_substring_pollution():
    """o1/o3 仅 2 字符，不应让 `kobold-o1-instruct` 之类不相关名误命中 200K。"""
    # 不相关名碰巧含 "o1" 子串，必须落入默认值而不是 o1 的 200000
    assert resolve_context_window("kobold-o1-instruct") == DEFAULT_CONTEXT_WINDOW
    # 但精确匹配仍正常
    assert resolve_context_window("o1") == 200_000
    assert resolve_context_window("o3") == 200_000
    # 长 pattern 仍能正常子串命中
    assert resolve_context_window("o1-mini-2024-09") == 128_000
    assert resolve_context_window("o3-mini-snapshot") == 200_000


# ============================================================
# 12. gpt-4-turbo 日期版本
# ============================================================
def test_gpt_4_turbo_dated():
    assert resolve_context_window("gpt-4-turbo-2024-04-09") == 128_000
    assert resolve_context_window("gpt-4-turbo-preview") == 128_000

