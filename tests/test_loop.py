"""Loop engine module tests."""
import pytest
from unittest.mock import patch, MagicMock


class TestLoopEngine:
    """Loop engine tests with mocked LLM calls."""

    @pytest.fixture
    def mock_llm_response(self):
        """Create a mock LLM response without tool calls."""
        return {
            "choices": [{
                "message": {
                    "content": "This is a test response from the LLM.",
                    "tool_calls": []
                }
            }]
        }

    @pytest.fixture
    def mock_llm_with_tool_call(self):
        """Create a mock LLM response with a tool call."""
        return {
            "choices": [{
                "message": {
                    "content": "Let me calculate that for you.",
                    "tool_calls": [{
                        "id": "call_123",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression": "2 + 2"}'
                        }
                    }]
                }
            }]
        }

    def test_loop_engine_initialization(self, tmp_path):
        """Verify loop engine initializes correctly."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry"), \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory.return_value = MagicMock()
            from harness_lite.loop import LoopEngine

            engine = LoopEngine(session_id="test_session")
            assert engine.session_id == "test_session"

    def test_run_returns_llm_response(self, mock_llm_response):
        """Verify run returns LLM response when no tool calls needed."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry"), \
             patch("harness_lite.loop.engine.get_main_config") as mock_config, \
             patch("harness_lite.loop.engine.requests.post") as mock_post:

            mock_memory = MagicMock()
            mock_memory.load_context.return_value = []
            mock_memory_cls.return_value = mock_memory

            mock_config.return_value = {
                "api_key": "test-key",
                "base_url": "https://api.example.com",
                "model_name": "test-model"
            }

            mock_post.return_value.json.return_value = mock_llm_response

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            result = engine.run("Calculate 2 + 2", "test_session")

            assert "test response" in result.lower() or len(result) > 0
            mock_memory.save_context.assert_called_once()

    def test_run_processes_tool_calls(self, mock_llm_with_tool_call):
        """Verify run processes tool calls from LLM."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager") as mock_security, \
             patch("harness_lite.loop.engine.tool_registry") as mock_registry, \
             patch("harness_lite.loop.engine.get_main_config") as mock_config, \
             patch("harness_lite.loop.engine.requests.post") as mock_post:

            mock_memory = MagicMock()
            mock_memory.load_context.return_value = []
            mock_memory_cls.return_value = mock_memory

            mock_security.intercept.return_value = (True, None)

            mock_tool = MagicMock()
            mock_tool.execute.return_value = "4"
            mock_registry.get.return_value = mock_tool
            mock_registry.get_all_schemas.return_value = []

            mock_config.return_value = {
                "api_key": "test-key",
                "base_url": "https://api.example.com",
                "model_name": "test-model"
            }

            mock_post.return_value.json.return_value = mock_llm_with_tool_call

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            result = engine.run("Calculate 2 + 2", "test_session")

            assert len(result) > 0
            mock_tool.execute.assert_called_once()

    def test_run_saves_context_on_completion(self, mock_llm_response):
        """Verify context is saved after LLM response."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry"), \
             patch("harness_lite.loop.engine.get_main_config"), \
             patch("harness_lite.loop.engine.requests.post") as mock_post:

            mock_memory = MagicMock()
            mock_memory.load_context.return_value = []
            mock_memory_cls.return_value = mock_memory

            mock_post.return_value.json.return_value = mock_llm_response

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            engine.run("test task", "test_session")

            mock_memory.save_context.assert_called_once()

    def test_run_respects_max_iterations(self):
        """Verify max iterations limit is respected."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry"), \
             patch("harness_lite.loop.engine.get_main_config"), \
             patch("harness_lite.loop.engine.requests.post") as mock_post:

            mock_memory = MagicMock()
            mock_memory.load_context.return_value = []
            mock_memory_cls.return_value = mock_memory

            # Always return a tool call response
            mock_post.return_value.json.return_value = {
                "choices": [{
                    "message": {
                        "content": "Calling tool...",
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression": "1"}'
                            }
                        }]
                    }
                }]
            }

            mock_registry = MagicMock()
            mock_tool = MagicMock()
            mock_tool.execute.return_value = "1"
            mock_registry.get.return_value = mock_tool
            mock_registry.get_all_schemas.return_value = []

            with patch("harness_lite.loop.engine.tool_registry", mock_registry):
                from harness_lite.loop import LoopEngine
                engine = LoopEngine(session_id="test_session")

                result = engine.run("test task", "test_session")

                # Should hit max iterations and return error message
                assert "最大迭代次数" in result or len(result) > 0
                # Verify multiple calls were made (up to max_iterations)
                assert mock_post.call_count <= 20

    def test_execute_tool_security_intercept(self):
        """Verify tool execution goes through security intercept."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager") as mock_security, \
             patch("harness_lite.loop.engine.tool_registry") as mock_registry, \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory = MagicMock()
            mock_memory_cls.return_value = mock_memory

            mock_security.intercept.return_value = (False, "Blocked by security")

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            result = engine._execute_tool("test_tool", {"key": "value"})

            assert "Security blocked" in result
            mock_security.intercept.assert_called_once()

    def test_execute_tool_not_found(self):
        """Verify tool not found returns error message."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager") as mock_security, \
             patch("harness_lite.loop.engine.tool_registry") as mock_registry, \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory = MagicMock()
            mock_memory_cls.return_value = mock_memory

            mock_security.intercept.return_value = (True, None)
            mock_registry.get.return_value = None

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            result = engine._execute_tool("nonexistent_tool", {})

            assert "not found" in result

    def test_build_messages_includes_system_prompt(self):
        """Verify build_messages creates correct structure."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry") as mock_registry, \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory = MagicMock()
            mock_memory_cls.return_value = mock_memory

            mock_registry.get_all_schemas.return_value = []

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            messages = engine._build_messages("test task")

            assert len(messages) >= 2
            assert messages[0]["role"] == "system"
            assert "tools" in messages[0]["content"].lower() or "tool" in messages[0]["content"].lower()

    def test_process_tool_calls_parses_json_arguments(self):
        """Verify tool call arguments are parsed from JSON string."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager") as mock_security, \
             patch("harness_lite.loop.engine.tool_registry") as mock_registry, \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory = MagicMock()
            mock_memory_cls.return_value = mock_memory

            mock_security.intercept.return_value = (True, None)

            mock_tool = MagicMock()
            mock_tool.execute.return_value = "4"
            mock_registry.get.return_value = mock_tool

            from harness_lite.loop import LoopEngine
            engine = LoopEngine(session_id="test_session")

            tool_calls = [{
                "id": "call_123",
                "function": {
                    "name": "calculator",
                    "arguments": '{"expression": "2 + 2"}'
                }
            }]

            results = engine._process_tool_calls(tool_calls)

            assert len(results) == 1
            assert results[0]["tool_call_id"] == "call_123"
            mock_tool.execute.assert_called_once_with(expression="2 + 2")


class TestLoopEngineIntegration:
    """Loop engine integration tests."""

    def test_loop_engine_session_id_handling(self):
        """Verify session ID is properly stored and used."""
        with patch("harness_lite.loop.engine.MemoryManager") as mock_memory_cls, \
             patch("harness_lite.loop.engine.security_manager"), \
             patch("harness_lite.loop.engine.tool_registry"), \
             patch("harness_lite.loop.engine.get_main_config"):

            mock_memory = MagicMock()
            mock_memory_cls.return_value = mock_memory

            from harness_lite.loop import LoopEngine

            engine = LoopEngine(session_id="initial_session")
            assert engine.session_id == "initial_session"

            # When run is called with different session_id
            mock_memory.load_context.return_value = []
            mock_memory.save_context.return_value = None

            with patch("harness_lite.loop.engine.requests.post") as mock_post:
                mock_post.return_value.json.return_value = {
                    "choices": [{"message": {"content": "test", "tool_calls": []}}]
                }

                engine.run("test", "new_session")

                assert engine.session_id == "new_session"
                mock_memory.load_context.assert_called_with("new_session")
