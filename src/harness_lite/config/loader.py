"""
Configuration loader module.

支持主 / 中 / 小三模型分层独立配置：
- 主模型 (LLM_MAIN_*) 必填，缺失任一字段抛 ValueError
- 中模型 (LLM_MEDIUM_*) 可选，缺失任一字段自动降级到主模型并写入 info 日志
- 小模型 (LLM_SMALL_*) 可选，规则同上
- thinking_mode 三组各自可配（LLM_*_THINKING_MODE）；中/小未显式设置时继承主模型

对外提供：get_main_config / get_medium_config / get_small_config / get_llm_config(别名) / reload_config
"""
from typing import Dict, Optional
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger("harness_lite.config")

# 三组配置独立缓存，reload_config 必须同时清空
_MAIN_CONFIG: Optional[Dict[str, any]] = None
_MEDIUM_CONFIG: Optional[Dict[str, any]] = None
_SMALL_CONFIG: Optional[Dict[str, any]] = None
_ENV_PATH: Optional[Path] = None


def _get_env_path() -> Path:
    """Get the path to the .env file in project root."""
    current = Path(__file__).resolve()
    # config -> harness_lite -> src -> project root
    project_root = current.parent.parent.parent.parent
    return project_root / ".env"


def _load_env() -> None:
    """从 .env 一次性加载并解析三组模型配置；解析后写入三组全局缓存。"""
    global _MAIN_CONFIG, _MEDIUM_CONFIG, _SMALL_CONFIG, _ENV_PATH
    _ENV_PATH = _get_env_path()

    if not _ENV_PATH.exists():
        raise ValueError(
            f".env file not found at {_ENV_PATH}. "
            "请创建 .env 并配置 LLM_MAIN_API_KEY / LLM_MAIN_BASE_URL / LLM_MAIN_MODEL_NAME。"
        )

    load_dotenv(_ENV_PATH, override=True)

    # ---- 1. 解析主模型（必填） ----
    main_api_key = os.getenv("LLM_MAIN_API_KEY")
    main_base_url = os.getenv("LLM_MAIN_BASE_URL")
    main_model_name = os.getenv("LLM_MAIN_MODEL_NAME")
    thinking_mode = os.getenv("LLM_MAIN_THINKING_MODE", "false").lower() == "true"

    missing = []
    if not main_api_key:
        missing.append("LLM_MAIN_API_KEY")
    if not main_base_url:
        missing.append("LLM_MAIN_BASE_URL")
    if not main_model_name:
        missing.append("LLM_MAIN_MODEL_NAME")
    if missing:
        raise ValueError(
            f"主模型配置缺失: {', '.join(missing)}。"
            f"请在 {_ENV_PATH} 中补全这些字段。"
        )

    _MAIN_CONFIG = {
        "api_key": main_api_key,
        "base_url": main_base_url,
        "model_name": main_model_name,
        "thinking_mode": thinking_mode,
    }

    # ---- 2. 解析中模型（缺失则降级到主模型） ----
    _MEDIUM_CONFIG = _resolve_tier_config(
        tier_label="medium",
        api_key=os.getenv("LLM_MEDIUM_API_KEY"),
        base_url=os.getenv("LLM_MEDIUM_BASE_URL"),
        model_name=os.getenv("LLM_MEDIUM_MODEL_NAME"),
        tier_thinking_raw=os.getenv("LLM_MEDIUM_THINKING_MODE"),
        main_thinking_mode=thinking_mode,
    )

    # ---- 3. 解析小模型（缺失则降级到主模型） ----
    _SMALL_CONFIG = _resolve_tier_config(
        tier_label="small",
        api_key=os.getenv("LLM_SMALL_API_KEY"),
        base_url=os.getenv("LLM_SMALL_BASE_URL"),
        model_name=os.getenv("LLM_SMALL_MODEL_NAME"),
        tier_thinking_raw=os.getenv("LLM_SMALL_THINKING_MODE"),
        main_thinking_mode=thinking_mode,
    )


def _resolve_tier_config(
    tier_label: str,
    api_key: Optional[str],
    base_url: Optional[str],
    model_name: Optional[str],
    tier_thinking_raw: Optional[str],
    main_thinking_mode: bool,
) -> Dict[str, any]:
    """解析单个非主模型层级；缺任一必填字段则降级到主模型。

    thinking_mode 规则：
    - 该层级显式设置 LLM_*_THINKING_MODE 时按其值（true/false）
    - 未设置则继承主模型的 thinking_mode
    """
    if not api_key or not base_url or not model_name:
        logger.info(f"{tier_label} 模型未完整配置，降级到 main")
        return _MAIN_CONFIG.copy()

    if tier_thinking_raw is None or tier_thinking_raw.strip() == "":
        tier_thinking_mode = main_thinking_mode
    else:
        tier_thinking_mode = tier_thinking_raw.strip().lower() == "true"

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model_name": model_name,
        "thinking_mode": tier_thinking_mode,
    }


def get_main_config() -> Dict[str, any]:
    """获取主模型配置；首次调用会触发 .env 加载。"""
    if _MAIN_CONFIG is None:
        _load_env()
    return _MAIN_CONFIG.copy()


def get_medium_config() -> Dict[str, any]:
    """获取中模型配置；未配置时降级返回主模型配置。"""
    if _MEDIUM_CONFIG is None:
        _load_env()
    return _MEDIUM_CONFIG.copy()


def get_small_config() -> Dict[str, any]:
    """获取小模型配置；未配置时降级返回主模型配置。"""
    if _SMALL_CONFIG is None:
        _load_env()
    return _SMALL_CONFIG.copy()


def get_llm_config() -> Dict[str, any]:
    """【向后兼容别名】等价于 get_main_config()，保留旧调用路径不中断。"""
    return get_main_config()


def reload_config() -> None:
    """清空三组缓存并重新加载 .env。"""
    global _MAIN_CONFIG, _MEDIUM_CONFIG, _SMALL_CONFIG
    _MAIN_CONFIG = None
    _MEDIUM_CONFIG = None
    _SMALL_CONFIG = None
    _load_env()
