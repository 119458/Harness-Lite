"""Configuration module tests."""
import os
import pytest
from pathlib import Path


class TestConfig:
    """Configuration management tests."""

    def test_get_llm_config_returns_dict(self, tmp_path):
        """Verify returned dictionary format with required keys."""
        # Create a temporary .env file for testing
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LLM_API_KEY=test-key-123\n"
            "LLM_BASE_URL=https://api.example.com\n"
            "LLM_MODEL_NAME=test-model\n"
        )

        # Set environment variable to point to our test .env
        os.environ["LLM_ENV_PATH"] = str(env_file)

        # Import after setting up test env
        from harness_lite.config.loader import _load_env, get_llm_config, reload_config

        # Reset global state
        import harness_lite.config.loader as loader_module
        loader_module._CONFIG = None
        loader_module._ENV_PATH = None

        # Patch _get_env_path to return our test path
        original_get_env_path = loader_module._get_env_path
        loader_module._get_env_path = lambda: env_file

        try:
            config = get_llm_config()
            assert isinstance(config, dict)
            assert "api_key" in config
            assert "base_url" in config
            assert "model_name" in config
        finally:
            loader_module._get_env_path = original_get_env_path
            loader_module._CONFIG = None
            loader_module._ENV_PATH = None
            if "LLM_ENV_PATH" in os.environ:
                del os.environ["LLM_ENV_PATH"]

    def test_config_values_not_empty(self, tmp_path):
        """Verify configuration values are not empty."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LLM_API_KEY=test-key-123\n"
            "LLM_BASE_URL=https://api.example.com\n"
            "LLM_MODEL_NAME=test-model\n"
        )

        import harness_lite.config.loader as loader_module
        original_get_env_path = loader_module._get_env_path
        loader_module._get_env_path = lambda: env_file
        loader_module._CONFIG = None
        loader_module._ENV_PATH = None

        try:
            from harness_lite.config.loader import get_llm_config
            config = get_llm_config()
            assert config["api_key"]
            assert config["base_url"]
            assert config["model_name"]
        finally:
            loader_module._get_env_path = original_get_env_path
            loader_module._CONFIG = None
            loader_module._ENV_PATH = None

    def test_reload_config(self, tmp_path):
        """Verify configuration reload works correctly."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LLM_API_KEY=test-key-123\n"
            "LLM_BASE_URL=https://api.example.com\n"
            "LLM_MODEL_NAME=test-model\n"
        )

        import harness_lite.config.loader as loader_module
        original_get_env_path = loader_module._get_env_path
        loader_module._get_env_path = lambda: env_file
        loader_module._CONFIG = None
        loader_module._ENV_PATH = None

        try:
            from harness_lite.config.loader import get_llm_config, reload_config
            reload_config()
            config = get_llm_config()
            assert config is not None
            assert isinstance(config, dict)
        finally:
            loader_module._get_env_path = original_get_env_path
            loader_module._CONFIG = None
            loader_module._ENV_PATH = None

    def test_missing_required_config_raises(self, tmp_path):
        """Verify ValueError is raised when required config is missing."""
        # Create .env with missing API key
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LLM_BASE_URL=https://api.example.com\n"
            "LLM_MODEL_NAME=test-model\n"
        )

        import harness_lite.config.loader as loader_module
        original_get_env_path = loader_module._get_env_path
        loader_module._get_env_path = lambda: env_file
        loader_module._CONFIG = None
        loader_module._ENV_PATH = None

        try:
            from harness_lite.config.loader import get_llm_config
            with pytest.raises(ValueError, match="Missing required configuration"):
                get_llm_config()
        finally:
            loader_module._get_env_path = original_get_env_path
            loader_module._CONFIG = None
            loader_module._ENV_PATH = None

    def test_missing_env_file_raises(self, tmp_path):
        """Verify ValueError is raised when .env file does not exist."""
        nonexistent_env = tmp_path / "nonexistent.env"

        import harness_lite.config.loader as loader_module
        original_get_env_path = loader_module._get_env_path
        loader_module._get_env_path = lambda: nonexistent_env
        loader_module._CONFIG = None
        loader_module._ENV_PATH = None

        try:
            from harness_lite.config.loader import get_llm_config
            with pytest.raises(ValueError, match=".env file not found"):
                get_llm_config()
        finally:
            loader_module._get_env_path = original_get_env_path
            loader_module._CONFIG = None
            loader_module._ENV_PATH = None
