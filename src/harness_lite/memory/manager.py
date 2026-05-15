"""Memory manager module.

Provides unified interface for managing conversation memories.
"""
from typing import List, Dict, Any

from .store import MemoryStore


class MemoryManager:
    """Memory manager exposing unified interface for memory operations."""

    def __init__(self, store_dir: str = "./memory_store"):
        """
        Initialize the memory manager.

        Args:
            store_dir: Directory path for storing session JSON files
        """
        self._store = MemoryStore(store_dir=store_dir)

    def save_context(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Save conversation context.

        Args:
            session_id: Session identifier
            messages: List of message dictionaries in format:
                      [{"role": "user"|"assistant", "content": "..."}, ...]
        """
        self._store.save(session_id, messages)

    def load_context(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load conversation context.

        Args:
            session_id: Session identifier

        Returns:
            List of message dictionaries
        """
        return self._store.load(session_id)

    def trim_history(self, session_id: str, keep_last_n: int) -> None:
        """
        Trim history messages, keeping only the last N messages.

        Args:
            session_id: Session identifier
            keep_last_n: Number of messages to keep
        """
        messages = self._store.load(session_id)
        if len(messages) > keep_last_n:
            trimmed = messages[-keep_last_n:]
            self._store.save(session_id, trimmed)

    def clear_context(self, session_id: str) -> None:
        """
        Clear memory for a session.

        Args:
            session_id: Session identifier
        """
        self._store.delete(session_id)

    def list_sessions(self) -> List[str]:
        """
        List all session IDs.

        Returns:
            List of session identifiers
        """
        return [
            f.stem for f in self._store._store_dir.iterdir()
            if f.suffix == ".json"
        ]
