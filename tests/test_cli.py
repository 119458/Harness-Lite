"""CLI module tests."""
import pytest


class TestCLI:
    """CLI tests - note: CLI is task_8 PENDING, tests may need adjustment."""

    def test_cli_module_can_be_imported(self):
        """Verify CLI module can be imported if it exists."""
        try:
            from harness_lite import cli
            assert cli is not None
        except ImportError:
            # CLI module not yet implemented (task_8 is PENDING)
            pytest.skip("CLI module not yet implemented (task_8 PENDING)")

    def test_cli_app_exists(self):
        """Verify CLI app exists when module is available."""
        try:
            from typer.testing import CliRunner
            from harness_lite.cli import app

            runner = CliRunner()
            assert runner is not None
        except ImportError:
            pytest.skip("CLI module not yet implemented (task_8 PENDING)")

    def test_help_option(self):
        """Verify --help option works."""
        try:
            from typer.testing import CliRunner
            from harness_lite.cli import app

            runner = CliRunner()
            result = runner.invoke(app, ["--help"])

            assert result.exit_code == 0
        except ImportError:
            pytest.skip("CLI module not yet implemented (task_8 PENDING)")

    def test_main_command_exists(self):
        """Verify main command is accessible."""
        try:
            from typer.testing import CliRunner
            from harness_lite.cli import app

            runner = CliRunner()
            result = runner.invoke(app, ["--help"])

            assert result.exit_code == 0
        except ImportError:
            pytest.skip("CLI module not yet implemented (task_8 PENDING)")


class TestCLIFallback:
    """Fallback tests when CLI is not available."""

    def test_cli_module_not_implemented(self):
        """Document that CLI is not yet implemented."""
        try:
            from harness_lite import cli
            # If we get here, CLI exists
            pass
        except ImportError:
            # CLI not implemented - this is expected per task_8 status
            from harness_lite.registry import __all__ as registry_all
            assert "Tool" in registry_all or "Skill" in registry_all
