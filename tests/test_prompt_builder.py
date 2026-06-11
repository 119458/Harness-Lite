"""PromptBuilder 单元测试。

覆盖：
- DYNAMIC_BOUNDARY 出现位置与次数
- 静态 section 缓存命中（compute 仅被调用一次）
- 动态 section 在依赖变更后缓存失效
- 整段 system 文本不出现任何关于其他厂牌助手的字样
- 单 section 抛错时 builder 不崩溃
"""

from __future__ import annotations

import re
from typing import Optional

import pytest

from harness_lite.prompt import DYNAMIC_BOUNDARY, PromptBuilder, PromptContext
from harness_lite.prompt.section_cache import SectionCache
from harness_lite.prompt.sections import environment as env_section


def _make_ctx(**overrides) -> PromptContext:
    base = dict(
        task="t",
        session_id="sid-001",
        model_name="some-model",
        sandbox_roots=("/tmp/sandbox-a",),
        enabled_tools=("calculator", "read_file"),
        tools_schema_json='[{"type":"function","function":{"name":"calculator"}}]',
        skills_list=({"name": "demo", "description": "示例技能"},),
        memory_text="# demo memory",
        mem0_enabled=False,
        cwd="/home/u",
        is_git=True,
        platform="Darwin",
        shell="/bin/zsh",
        os_version="24.6.0",
        current_date="2026/06/11",
        thinking_mode=False,
    )
    base.update(overrides)
    return PromptContext(**base)


def test_boundary_appears_exactly_once():
    cache = SectionCache()
    text = PromptBuilder(_make_ctx(), cache).build()
    assert text.count(DYNAMIC_BOUNDARY) == 1
    # 静态部分应位于 boundary 之前
    assert text.index("# 任务执行") < text.index(DYNAMIC_BOUNDARY)
    # 动态部分应位于 boundary 之后
    assert text.index(DYNAMIC_BOUNDARY) < text.index("# 环境")


def test_static_section_cache_hits_on_same_ctx(monkeypatch):
    """同 ctx 二次 build 时静态 section 的 compute 不应再次被调用。"""
    cache = SectionCache()
    from harness_lite.prompt.sections import intro

    call_counter = {"n": 0}
    original_compute = intro.compute
    original_dep_sig = original_compute.dep_sig

    def wrapped(ctx):
        call_counter["n"] += 1
        return original_compute(ctx)

    wrapped.dep_sig = original_dep_sig
    monkeypatch.setattr(intro, "compute", wrapped)
    # 重新注册到 builder（注册表在 import 时已绑定原函数，需要手动替换）
    monkeypatch.setattr(
        PromptBuilder,
        "STATIC_SECTIONS",
        [(name, wrapped if name == "intro" else fn) for name, fn in PromptBuilder.STATIC_SECTIONS],
    )

    ctx = _make_ctx()
    PromptBuilder(ctx, cache).build()
    PromptBuilder(ctx, cache).build()
    assert call_counter["n"] == 1, "intro.compute 应只被调用一次（第二次走缓存）"


def test_environment_cache_invalidated_when_sandbox_changes():
    """sandbox_roots 变更应让 environment section 重新计算。"""
    cache = SectionCache()
    ctx_a = _make_ctx(sandbox_roots=("/tmp/sb-a",))
    ctx_b = _make_ctx(sandbox_roots=("/tmp/sb-b",))

    sig_a = env_section.dep_sig(ctx_a)
    sig_b = env_section.dep_sig(ctx_b)
    assert sig_a != sig_b, "不同 sandbox_roots 应产生不同 dep_sig"

    text_a = PromptBuilder(ctx_a, cache).build()
    text_b = PromptBuilder(ctx_b, cache).build()
    assert "/tmp/sb-a" in text_a and "/tmp/sb-a" not in text_b
    assert "/tmp/sb-b" in text_b and "/tmp/sb-b" not in text_a


def test_no_third_party_assistant_brand_words():
    """生成的 prompt 不应包含其它厂牌助手字样。"""
    cache = SectionCache()
    text = PromptBuilder(_make_ctx(), cache).build()
    forbidden = re.compile(r"claude|anthropic|claude\.ai", re.IGNORECASE)
    match = forbidden.search(text)
    assert match is None, f"出现禁用字样: {match.group(0) if match else ''}"


def test_section_exception_does_not_break_build(monkeypatch):
    """单个 section 抛错时 builder 应仅跳过该段并继续。"""
    cache = SectionCache()
    from harness_lite.prompt.sections import tone_style

    def boom(_ctx):
        raise RuntimeError("simulated section failure")

    boom.dep_sig = lambda _ctx: "tone_style:boom"
    monkeypatch.setattr(tone_style, "compute", boom)
    monkeypatch.setattr(
        PromptBuilder,
        "STATIC_SECTIONS",
        [(name, boom if name == "tone_style" else fn) for name, fn in PromptBuilder.STATIC_SECTIONS],
    )

    text = PromptBuilder(_make_ctx(), cache).build()
    # tone_style 段缺失但其余 section 仍在
    assert "# 沟通风格" not in text
    assert "# 任务执行" in text
    assert "# 环境" in text
    assert DYNAMIC_BOUNDARY in text


def test_skills_catalog_handles_empty_skills():
    """技能列表为空时应优雅渲染兜底文本。"""
    cache = SectionCache()
    text = PromptBuilder(_make_ctx(skills_list=()), cache).build()
    assert "（若为空：当前未加载任何业务技能。）" in text


def test_memory_section_reflects_mem0_toggle():
    """mem0 开关切换时 memory_recall dep_sig 应不同。"""
    from harness_lite.prompt.sections import memory_recall

    ctx_off = _make_ctx(mem0_enabled=False)
    ctx_on = _make_ctx(mem0_enabled=True)
    assert memory_recall.dep_sig(ctx_off) != memory_recall.dep_sig(ctx_on)
