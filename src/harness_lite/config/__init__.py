"""Configuration module for Harness-Lite.

提供主 / 中 / 小三层 LLM 模型配置管理；get_llm_config 作为别名指向 get_main_config。
"""
from .loader import (
    get_main_config,
    get_medium_config,
    get_small_config,
    get_llm_config,
    reload_config,
)

__all__ = [
    "get_main_config",
    "get_medium_config",
    "get_small_config",
    "get_llm_config",
    "reload_config",
]
