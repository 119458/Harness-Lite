"""三模型分层配置加载器单元测试。

覆盖 7 个场景：
1. 主模型返回四字段
2. 仅配 main 时 medium 降级到 main
3. 配齐三组时 medium/small 独立
4. 小模型部分缺失时降级到 main
5. 主模型字段缺失抛 ValueError 且消息包含 LLM_MAIN_* 字段名
6. get_llm_config 别名等价于 get_main_config
7. reload_config 清空三组缓存
"""
from __future__ import annotations

import pytest

import harness_lite.config.loader as loader
from harness_lite.config.loader import (
    get_main_config,
    get_medium_config,
    get_small_config,
    get_llm_config,
    reload_config,
)


def _write_env(tmp_path, body: str):
    """生成临时 .env 并将 loader._get_env_path 指向它。"""
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """每个用例独立 .env + 清空全局缓存，避免相互污染。"""
    env_file = tmp_path / ".env"
    env_file.touch()
    monkeypatch.setattr(loader, "_get_env_path", lambda: env_file)

    # 强制清空三组缓存
    loader._MAIN_CONFIG = None
    loader._MEDIUM_CONFIG = None
    loader._SMALL_CONFIG = None

    # 清理任何残留 LLM_* 环境变量（含 MAX_TOKENS，避免污染 max_context_tokens 解析）
    for prefix in ("LLM_MAIN_", "LLM_MEDIUM_", "LLM_SMALL_"):
        for suffix in ("API_KEY", "BASE_URL", "MODEL_NAME", "THINKING_MODE", "MAX_TOKENS"):
            monkeypatch.delenv(f"{prefix}{suffix}", raising=False)

    yield env_file

    loader._MAIN_CONFIG = None
    loader._MEDIUM_CONFIG = None
    loader._SMALL_CONFIG = None


# ============================================================
# 1. 主模型返回四字段
# ============================================================
def test_main_config_returns_required_fields(_isolate_env):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=main-model\n"
        "LLM_MAIN_THINKING_MODE=true\n",
        encoding="utf-8",
    )
    reload_config()

    config = get_main_config()
    assert config["api_key"] == "main-key"
    assert config["base_url"] == "https://main.example.com"
    assert config["model_name"] == "main-model"
    assert config["thinking_mode"] is True


# ============================================================
# 2. 仅配 main 时 medium 降级到 main
# ============================================================
def test_medium_config_fallback(_isolate_env, caplog):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=main-model\n",
        encoding="utf-8",
    )

    with caplog.at_level("INFO", logger="harness_lite.config"):
        reload_config()
        medium = get_medium_config()
        main = get_main_config()

    assert medium["model_name"] == main["model_name"]
    assert medium["api_key"] == main["api_key"]
    assert medium["base_url"] == main["base_url"]
    assert any("medium 模型未完整配置，降级到 main" in rec.message for rec in caplog.records)


# ============================================================
# 3. 配齐三组时 medium/small 独立
# ============================================================
def test_medium_config_independent(_isolate_env):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=main-model\n"
        "LLM_MAIN_THINKING_MODE=false\n"
        "LLM_MEDIUM_API_KEY=medium-key\n"
        "LLM_MEDIUM_BASE_URL=https://medium.example.com\n"
        "LLM_MEDIUM_MODEL_NAME=medium-model\n"
        "LLM_MEDIUM_THINKING_MODE=true\n"
        "LLM_SMALL_API_KEY=small-key\n"
        "LLM_SMALL_BASE_URL=https://small.example.com\n"
        "LLM_SMALL_MODEL_NAME=small-model\n"
        "LLM_SMALL_THINKING_MODE=false\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    medium = get_medium_config()
    small = get_small_config()

    assert medium["model_name"] == "medium-model"
    assert medium["api_key"] == "medium-key"
    assert medium["base_url"] == "https://medium.example.com"
    assert small["model_name"] == "small-model"
    assert small["api_key"] == "small-key"

    # thinking_mode 三组独立：main=false / medium=true / small=false
    assert main["thinking_mode"] is False
    assert medium["thinking_mode"] is True
    assert small["thinking_mode"] is False

    # 三组互不相同
    assert medium["model_name"] != main["model_name"]
    assert small["model_name"] != main["model_name"]
    assert medium["model_name"] != small["model_name"]


# ============================================================
# 4. 小模型部分缺失时降级
# ============================================================
def test_small_config_fallback(_isolate_env, caplog):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=main-model\n"
        # 小模型只配置了 key 和 url, 缺 model_name → 整组降级
        "LLM_SMALL_API_KEY=small-key\n"
        "LLM_SMALL_BASE_URL=https://small.example.com\n",
        encoding="utf-8",
    )

    with caplog.at_level("INFO", logger="harness_lite.config"):
        reload_config()
        small = get_small_config()
        main = get_main_config()

    assert small["model_name"] == main["model_name"]
    assert small["api_key"] == main["api_key"]
    assert any("small 模型未完整配置，降级到 main" in rec.message for rec in caplog.records)


# ============================================================
# 5. 主模型字段缺失抛 ValueError，消息含新字段名
# ============================================================
def test_main_config_missing_raises_value_error(_isolate_env):
    _isolate_env.write_text(
        # 故意只写 BASE_URL，缺 API_KEY 与 MODEL_NAME
        "LLM_MAIN_BASE_URL=https://main.example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        reload_config()

    msg = str(exc_info.value)
    assert "LLM_MAIN_API_KEY" in msg
    assert "LLM_MAIN_MODEL_NAME" in msg
    # 确保错误消息不再误导用户使用旧字段
    assert "LLM_API_KEY" not in msg.replace("LLM_MAIN_API_KEY", "")


# ============================================================
# 6. get_llm_config 别名等价于 get_main_config
# ============================================================
def test_get_llm_config_alias(_isolate_env):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=alias-key\n"
        "LLM_MAIN_BASE_URL=https://alias.example.com\n"
        "LLM_MAIN_MODEL_NAME=alias-model\n",
        encoding="utf-8",
    )
    reload_config()

    legacy = get_llm_config()
    main = get_main_config()
    assert legacy == main


# ============================================================
# 6b. 中/小模型未显式设置 THINKING_MODE 时继承主模型
# ============================================================
def test_tier_thinking_mode_inherits_main_when_not_set(_isolate_env):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=main-model\n"
        "LLM_MAIN_THINKING_MODE=true\n"
        # medium 独立配置但不写 THINKING_MODE → 应继承 main 的 true
        "LLM_MEDIUM_API_KEY=medium-key\n"
        "LLM_MEDIUM_BASE_URL=https://medium.example.com\n"
        "LLM_MEDIUM_MODEL_NAME=medium-model\n"
        # small 独立配置但 THINKING_MODE 写为空值 → 也应继承 main
        "LLM_SMALL_API_KEY=small-key\n"
        "LLM_SMALL_BASE_URL=https://small.example.com\n"
        "LLM_SMALL_MODEL_NAME=small-model\n"
        "LLM_SMALL_THINKING_MODE=\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    medium = get_medium_config()
    small = get_small_config()

    assert main["thinking_mode"] is True
    assert medium["thinking_mode"] is True   # 缺字段 → 继承
    assert small["thinking_mode"] is True    # 空值 → 继承


# ============================================================
# 7. reload_config 必须清空三组缓存
# ============================================================
def test_reload_config_resets_all(_isolate_env):
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=v1-key\n"
        "LLM_MAIN_BASE_URL=https://v1.example.com\n"
        "LLM_MAIN_MODEL_NAME=v1-model\n",
        encoding="utf-8",
    )
    reload_config()
    assert get_main_config()["model_name"] == "v1-model"
    assert get_medium_config()["model_name"] == "v1-model"
    assert get_small_config()["model_name"] == "v1-model"

    # 改写 .env，再次 reload 应得到新值（验证缓存确实被清空）
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=v2-key\n"
        "LLM_MAIN_BASE_URL=https://v2.example.com\n"
        "LLM_MAIN_MODEL_NAME=v2-model\n",
        encoding="utf-8",
    )
    reload_config()
    assert get_main_config()["model_name"] == "v2-model"
    assert get_medium_config()["model_name"] == "v2-model"
    assert get_small_config()["model_name"] == "v2-model"

    # 显式验证三组缓存槽位都被实际刷新（非保留旧值）
    assert loader._MAIN_CONFIG is not None
    assert loader._MEDIUM_CONFIG is not None
    assert loader._SMALL_CONFIG is not None


# ============================================================
# 8. 主模型 max_context_tokens 由注册表解析
# ============================================================
def test_main_config_includes_max_context_tokens(_isolate_env):
    """deepseek-r1 在注册表中精确匹配为 128000。"""
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=deepseek-r1\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    assert main["max_context_tokens"] == 128_000


# ============================================================
# 9. LLM_MAIN_MAX_TOKENS 环境变量覆盖注册表
# ============================================================
def test_main_max_tokens_env_override(_isolate_env):
    """env 中显式设置 LLM_MAIN_MAX_TOKENS=64000，应覆盖注册表的 128000。"""
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=deepseek-r1\n"
        "LLM_MAIN_MAX_TOKENS=64000\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    assert main["max_context_tokens"] == 64_000


# ============================================================
# 10. 非法 LLM_MAIN_MAX_TOKENS 回退到注册表 + 写 warning
# ============================================================
def test_main_max_tokens_invalid_falls_back(_isolate_env, caplog):
    """LLM_MAIN_MAX_TOKENS=abc 无法解析 → 回退到注册表（128000）并记录 warning。"""
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=deepseek-r1\n"
        "LLM_MAIN_MAX_TOKENS=abc\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="harness_lite.config"):
        reload_config()
        main = get_main_config()

    assert main["max_context_tokens"] == 128_000
    assert any(
        "MAX_TOKENS" in rec.message and "abc" in rec.message
        for rec in caplog.records
    ), "应记录关于非法 MAX_TOKENS 的 warning"


# ============================================================
# 11. medium 降级时继承 main 的 max_context_tokens
# ============================================================
def test_medium_inherits_main_max_context_tokens(_isolate_env):
    """medium 未配置 → 整组降级到 main，max_context_tokens 也跟着继承。"""
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=gpt-4-32k\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    medium = get_medium_config()

    assert main["max_context_tokens"] == 32_768
    assert medium["max_context_tokens"] == 32_768


# ============================================================
# 12. medium 独立配置时按自身 model_name 解析 max_context_tokens
# ============================================================
def test_medium_independent_resolves_own_max_context_tokens(_isolate_env):
    """main=gpt-4-32k(32768) / medium=gpt-4o(128000)，各自独立解析。"""
    _isolate_env.write_text(
        "LLM_MAIN_API_KEY=main-key\n"
        "LLM_MAIN_BASE_URL=https://main.example.com\n"
        "LLM_MAIN_MODEL_NAME=gpt-4-32k\n"
        "LLM_MEDIUM_API_KEY=medium-key\n"
        "LLM_MEDIUM_BASE_URL=https://medium.example.com\n"
        "LLM_MEDIUM_MODEL_NAME=gpt-4o\n",
        encoding="utf-8",
    )
    reload_config()

    main = get_main_config()
    medium = get_medium_config()

    assert main["max_context_tokens"] == 32_768
    assert medium["max_context_tokens"] == 128_000
