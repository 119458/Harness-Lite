"""Configuration loader module.

Loads LLM configuration from .env file using python-dotenv.
"""
from typing import Dict
import os
from pathlib import Path
from dotenv import load_dotenv

_CONFIG: Dict[str, str] | None = None
_ENV_PATH: Path | None = None


def _get_env_path() -> Path:
    """Get the path to the .env file in project root."""
    # Start from this file's directory: src/harness_lite/config/
    # Go up to project root
    current = Path(__file__).resolve()
    # config -> harness_lite -> src -> project root
    project_root = current.parent.parent.parent.parent
    return project_root / ".env"


def _load_env() -> None:
    """Load environment variables from .env file."""
    global _CONFIG, _ENV_PATH
    _ENV_PATH = _get_env_path()

    if not _ENV_PATH.exists():
        raise ValueError(
            f".env file not found at {_ENV_PATH}. "
            "Please create a .env file with LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL_NAME."
        )

    load_dotenv(_ENV_PATH)

    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model_name = os.getenv("LLM_MODEL_NAME")

    # Validate required config
    missing = []
    if not api_key:
        missing.append("LLM_API_KEY")
    if not base_url:
        missing.append("LLM_BASE_URL")
    if not model_name:
        missing.append("LLM_MODEL_NAME")

    if missing:
        raise ValueError(
            f"Missing required configuration: {', '.join(missing)}. "
            f"Please set these values in {_ENV_PATH}."
        )

    _CONFIG = {
        "api_key": api_key,
        "base_url": base_url,
        "model_name": model_name,
    }


def get_llm_config() -> Dict[str, str]:
    """
    Get LLM configuration dictionary.

    Loads from .env file if not already loaded, then returns cached config.

    Returns:
        Dict[str, str]: Contains api_key, base_url, model_name

    Raises:
        ValueError: If .env file is missing or required config is absent
    """
    global _CONFIG
    if _CONFIG is None:
        _load_env()
    return _CONFIG.copy()


def reload_config() -> None:
    """Reload configuration from .env file."""
    global _CONFIG
    _CONFIG = None
    _load_env()
