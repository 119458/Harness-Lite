"""engine.build_initial_messages 兼容性测试。"""

from __future__ import annotations

import os

import pytest

# 确保配置加载不报错（测试环境用占位值）
os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("LLM_BASE_URL", "http://localhost")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")

from harness_lite import tools  # noqa: F401  触发工具自动注册
from harness_lite.loop.engine import AsyncLoopEngine
from harness_lite.security.manager import security_manager


@pytest.fixture()
def engine() -> AsyncLoopEngine:
    return AsyncLoopEngine()


def test_returns_system_then_user_pair(engine):
    msgs = engine.build_initial_messages("写一段 hello", "sess-x")
    assert isinstance(msgs, list) and len(msgs) == 2
    assert msgs[0]["role"] == "system" and isinstance(msgs[0]["content"], str)
    assert msgs[1] == {"role": "user", "content": "写一段 hello"}


def test_system_contains_sandbox_roots(engine):
    msgs = engine.build_initial_messages("task", "sid-1")
    text = msgs[0]["content"]
    roots = list(security_manager.active_sandbox_roots)
    assert roots, "测试前提：至少挂载一个沙箱"
    # 任意一个沙箱根的字符串都应该出现在 system 段中
    assert any(str(r) in text for r in roots)


def test_system_contains_registered_tool_name(engine):
    msgs = engine.build_initial_messages("task", "sid-2")
    text = msgs[0]["content"]
    # calculator 是项目内置最稳定的工具之一
    assert "calculator" in text


def test_system_does_not_use_third_party_brand_words(engine):
    msgs = engine.build_initial_messages("task", "sid-3")
    text = msgs[0]["content"].lower()
    assert "claude" not in text
    assert "anthropic" not in text
