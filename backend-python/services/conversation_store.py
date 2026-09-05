"""Persistent conversation memory and MCP execution traces."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.text_normalization import plain_speech_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ConversationStore:
    def __init__(self, database_path: str) -> None:
        path = Path(database_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_turns_device_created
                    ON conversation_turns(device_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS tool_traces (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_traces_turn_created
                    ON tool_traces(turn_id, created_at);
                """
            )
            # One-time/idempotent cleanup keeps legacy Markdown from being
            # shown on the dashboard or fed back to the next model request.
            rows = connection.execute(
                "SELECT id, assistant_text FROM conversation_turns WHERE assistant_text <> ''"
            ).fetchall()
            updates = [
                (cleaned, row["id"])
                for row in rows
                if (cleaned := plain_speech_text(row["assistant_text"])) != row["assistant_text"]
            ]
            if updates:
                connection.executemany(
                    "UPDATE conversation_turns SET assistant_text = ? WHERE id = ?", updates
                )

    async def create_turn(self, device_id: str, user_text: str, provider: str, model: str) -> str:
        turn_id = str(uuid.uuid4())
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO conversation_turns "
            "(id, device_id, user_text, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, device_id, user_text, provider, model, _now()),
        )
        return turn_id

    async def complete_turn(self, turn_id: str, assistant_text: str) -> None:
        assistant_text = plain_speech_text(assistant_text)
        await asyncio.to_thread(
            self._execute,
            "UPDATE conversation_turns SET assistant_text = ?, completed_at = ? WHERE id = ?",
            (assistant_text, _now(), turn_id),
        )

    async def update_execution(self, turn_id: str, provider: str, model: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE conversation_turns SET provider = ?, model = ? WHERE id = ?",
            (provider, model, turn_id),
        )

    async def add_tool_trace(
        self,
        turn_id: str,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        duration_ms: int,
        status: str,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO tool_traces "
            "(id, turn_id, name, arguments_json, result_json, duration_ms, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), turn_id, name,
                json.dumps(arguments, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                duration_ms, status, _now(),
            ),
        )

    def _execute(self, query: str, params: tuple[Any, ...]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(query, params)

    async def recent_context(self, device_id: str, limit: int = 12) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._recent_context_sync, device_id, limit)

    def _recent_context_sync(self, device_id: str, limit: int) -> list[dict[str, str]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT user_text, assistant_text FROM conversation_turns "
                "WHERE device_id = ? AND assistant_text <> '' ORDER BY created_at DESC LIMIT ?",
                (device_id, max(1, min(limit, 50))),
            ).fetchall()
        messages: list[dict[str, str]] = []
        for row in reversed(rows):
            messages.extend((
                {"role": "user", "content": row["user_text"]},
                {"role": "assistant", "content": plain_speech_text(row["assistant_text"])},
            ))
        return messages

    async def list_turns(self, device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_turns_sync, device_id, limit)

    def _list_turns_sync(self, device_id: str | None, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        query = "SELECT * FROM conversation_turns"
        params: tuple[Any, ...]
        if device_id:
            query += " WHERE device_id = ?"
            params = (device_id, limit)
        else:
            params = (limit,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with closing(self._connect()) as connection, connection:
            turns = [dict(row) for row in connection.execute(query, params).fetchall()]
            for turn in turns:
                turn["assistant_text"] = plain_speech_text(turn["assistant_text"])
                traces = connection.execute(
                    "SELECT * FROM tool_traces WHERE turn_id = ? ORDER BY created_at",
                    (turn["id"],),
                ).fetchall()
                turn["tool_calls"] = [self._decode_trace(dict(trace)) for trace in traces]
        return turns

    @staticmethod
    def _decode_trace(trace: dict[str, Any]) -> dict[str, Any]:
        trace["arguments"] = json.loads(trace.pop("arguments_json"))
        trace["result"] = json.loads(trace.pop("result_json"))
        return trace
