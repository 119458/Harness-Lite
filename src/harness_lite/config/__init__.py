"""Configuration module for Harness-Lite.

Provides centralized configuration management for LLM settings.
"""
from .loader import get_llm_config

__all__ = ["get_llm_config"]
