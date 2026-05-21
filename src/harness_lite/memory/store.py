"""Memory store module.

Provides JSON file-based storage for conversation memories.
"""
import json
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime


class MemoryStore:
    """Memory storage layer based on JSON files."""

    def __init__(self, store_dir: str = "./memory_store"):
        """
        Initialize the memory store.

        Args:
            store_dir: Directory path for storing session JSON files
        """
        self._store_dir = Path(store_dir)
        self._store_dir.mkdir(parents=True, exist_ok=True)

    def _get_date_dir(self) -> Path:
        """Get the directory for today's date, create if not exists."""
        today = datetime.now().strftime("%Y-%m-%d")
        date_dir = self._store_dir / today
        date_dir.mkdir(parents=True, exist_ok=True)
        return date_dir

    def _get_file_path(self, session_id: str) -> Path:
        """Get the file path for a session."""
        # Ensure session_id is a string
        session_id_str = str(session_id) if not isinstance(session_id, str) else session_id
        # Sanitize session_id to prevent path traversal
        safe_session_id = session_id_str.replace("/", "_").replace("\\", "_")
        # Store in date-based directory
        return self._get_date_dir() / f"{safe_session_id}.json"

    def save(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Save conversation messages to file.

        Args:
            session_id: Session identifier
            messages: List of message dictionaries
        """
        file_path = self._get_file_path(session_id)
        with open(file_path, "w", encoding="utf-8", errors="replace") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

    def load(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages for a session.

        Args:
            session_id: Session identifier

        Returns:
            List of message dictionaries, empty list if session not found
        """
        file_path = self._get_file_path(session_id)
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def delete(self, session_id: str) -> None:
        """
        Delete memory for a session.

        Args:
            session_id: Session identifier
        """
        file_path = self._get_file_path(session_id)
        if file_path.exists():
            file_path.unlink()

    def exists(self, session_id: str) -> bool:
        """
        Check if a session exists.

        Args:
            session_id: Session identifier

        Returns:
            True if session exists, False otherwise
        """
        return self._get_file_path(session_id).exists()

    def list_sessions(self) -> List[str]:
        """
        List all session IDs across all date directories.

        Returns:
            List of session identifiers
        """
        sessions = []
        for date_dir in self._store_dir.iterdir():
            if date_dir.is_dir():
                for json_file in date_dir.glob("*.json"):
                    sessions.append(json_file.stem)
        return sessions

    def list_dates(self) -> List[str]:
        """
        List all date directories.

        Returns:
            List of date strings (YYYY-MM-DD)
        """
        dates = []
        for date_dir in self._store_dir.iterdir():
            if date_dir.is_dir() and date_dir.name.startswith("20"):
                dates.append(date_dir.name)
        return sorted(dates)
