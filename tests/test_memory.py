"""Memory module tests."""
import pytest
import tempfile
import shutil
from pathlib import Path


class TestMemory:
    """Memory mechanism tests."""

    @pytest.fixture
    def memory_manager(self):
        """Create MemoryManager with temporary storage."""
        tmpdir = tempfile.mkdtemp()
        manager = MemoryManager(store_dir=tmpdir)
        yield manager
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_and_load_context(self, memory_manager):
        """Verify save and load functionality."""
        from harness_lite.memory import MemoryManager

        session_id = "test_session"
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"}
        ]
        memory_manager.save_context(session_id, messages)
        loaded = memory_manager.load_context(session_id)
        assert loaded == messages

    def test_load_nonexistent_returns_empty(self, memory_manager):
        """Verify loading nonexistent session returns empty list."""
        loaded = memory_manager.load_context("nonexistent")
        assert loaded == []

    def test_trim_history(self, memory_manager):
        """Verify history trimming keeps only last N messages."""
        session_id = "test_session"
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
        memory_manager.save_context(session_id, messages)
        memory_manager.trim_history(session_id, keep_last_n=3)
        loaded = memory_manager.load_context(session_id)
        assert len(loaded) == 3
        assert loaded[-1]["content"] == "msg9"

    def test_clear_context(self, memory_manager):
        """Verify clearing memory removes session data."""
        session_id = "test_session"
        messages = [{"role": "user", "content": "Hello"}]
        memory_manager.save_context(session_id, messages)
        memory_manager.clear_context(session_id)
        assert memory_manager.load_context(session_id) == []

    def test_list_sessions(self, memory_manager):
        """Verify session listing returns all session IDs."""
        memory_manager.save_context("session1", [{"role": "user", "content": "1"}])
        memory_manager.save_context("session2", [{"role": "user", "content": "2"}])
        sessions = memory_manager.list_sessions()
        assert "session1" in sessions
        assert "session2" in sessions

    def test_save_updates_existing_session(self, memory_manager):
        """Verify saving to existing session overwrites old data."""
        session_id = "test_session"
        messages1 = [{"role": "user", "content": "First message"}]
        messages2 = [{"role": "user", "content": "Second message"}]

        memory_manager.save_context(session_id, messages1)
        memory_manager.save_context(session_id, messages2)

        loaded = memory_manager.load_context(session_id)
        assert loaded == messages2
        assert loaded != messages1

    def test_trim_history_preserves_order(self, memory_manager):
        """Verify trimming preserves message order."""
        session_id = "test_session"
        messages = [
            {"role": "user", "content": "msg0"},
            {"role": "assistant", "content": "msg1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "msg3"},
            {"role": "user", "content": "msg4"},
        ]
        memory_manager.save_context(session_id, messages)
        memory_manager.trim_history(session_id, keep_last_n=2)

        loaded = memory_manager.load_context(session_id)
        assert len(loaded) == 2
        assert loaded[0]["content"] == "msg3"
        assert loaded[1]["content"] == "msg4"

    def test_trim_does_nothing_if_already_short(self, memory_manager):
        """Verify trim does nothing when message count <= keep_last_n."""
        session_id = "test_session"
        messages = [
            {"role": "user", "content": "msg0"},
            {"role": "user", "content": "msg1"},
        ]
        memory_manager.save_context(session_id, messages)
        memory_manager.trim_history(session_id, keep_last_n=5)

        loaded = memory_manager.load_context(session_id)
        assert len(loaded) == 2
