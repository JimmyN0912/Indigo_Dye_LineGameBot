import base64
import csv
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .game import Message

logger = logging.getLogger(__name__)


class UserStore:
    def __init__(self, database_path: str):
        self._database_path = Path(database_path)
        database_exists = self._database_path.exists()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if not database_exists:
            self._migrate_legacy_csv()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id TEXT PRIMARY KEY,
                    current_challenge_id INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    challenge_id INTEGER NOT NULL,
                    line_event_id TEXT,
                    message_type TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES players(user_id)
                );
                CREATE TABLE IF NOT EXISTS photo_submissions (
                    submission_id INTEGER PRIMARY KEY,
                    line_message_id TEXT NOT NULL,
                    local_path_or_url TEXT NOT NULL,
                    ai_result TEXT,
                    FOREIGN KEY (submission_id) REFERENCES submissions(id)
                );
                CREATE TABLE IF NOT EXISTS line_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_event_id TEXT,
                    user_id TEXT,
                    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_submissions_user_id ON submissions(user_id);
                CREATE INDEX IF NOT EXISTS idx_line_events_user_id ON line_events(user_id);
                """
            )
            for column_definition in (
                "story_index INTEGER NOT NULL DEFAULT 0",
                "hint_count INTEGER NOT NULL DEFAULT 0",
                "route_hint_count INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    connection.execute(f"ALTER TABLE players ADD COLUMN {column_definition}")
                except sqlite3.OperationalError:
                    pass

    def _migrate_legacy_csv(self) -> None:
        legacy_path = self._database_path.with_name("users.csv")
        if legacy_path == self._database_path or not legacy_path.exists():
            return
        with legacy_path.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        with self._connect() as connection:
            for row in rows:
                user_id = row.get("user_id")
                if user_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO players (user_id, current_challenge_id) VALUES (?, ?)",
                        (user_id, int(row.get("step_index") or 0)),
                    )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_step_index(self, user_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_challenge_id FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["current_challenge_id"]) if row else 0

    def set_step_index(self, user_id: str, step_index: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE players SET current_challenge_id = ?, story_index = 0, hint_count = 0, route_hint_count = 0, completed_at = NULL WHERE user_id = ?",
                (step_index, user_id),
            )

    def get_story_index(self, user_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT story_index FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["story_index"]) if row else 0

    def set_story_index(self, user_id: str, story_index: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE players SET story_index = ? WHERE user_id = ?",
                (story_index, user_id),
            )

    def increment_hint_count(self, user_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                "UPDATE players SET hint_count = hint_count + 1 WHERE user_id = ?",
                (user_id,),
            )
            row = connection.execute(
                "SELECT hint_count FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["hint_count"]) if row else 0

    def increment_route_hint_count(self, user_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                "UPDATE players SET route_hint_count = route_hint_count + 1 WHERE user_id = ?",
                (user_id,),
            )
            row = connection.execute(
                "SELECT route_hint_count FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["route_hint_count"]) if row else 0

    def mark_completed(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE players SET completed_at = ? WHERE user_id = ?", (self._now(), user_id)
            )

    def reset_user(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM photo_submissions
                WHERE submission_id IN (
                    SELECT id FROM submissions WHERE user_id = ?
                )
                """,
                (user_id,),
            )
            connection.execute("DELETE FROM submissions WHERE user_id = ?", (user_id,))
            connection.execute(
                "UPDATE players SET current_challenge_id = 0, story_index = 0, hint_count = 0, route_hint_count = 0, completed_at = NULL WHERE user_id = ?",
                (user_id,),
            )

    def reset_all(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE players SET current_challenge_id = 0, story_index = 0, hint_count = 0, route_hint_count = 0, completed_at = NULL")

    def has_user(self, user_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row is not None

    def add_user(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO players (user_id) VALUES (?)", (user_id,))

    def record_line_event(
        self, line_event_id: str | None, user_id: str | None, direction: str, payload: Any
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO line_events (line_event_id, user_id, direction, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (line_event_id, user_id, direction, json.dumps(payload, ensure_ascii=False), self._now()),
            )

    def has_line_event(self, line_event_id: str | None) -> bool:
        if not line_event_id:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM line_events WHERE line_event_id = ? AND direction = 'inbound' LIMIT 1",
                (line_event_id,),
            ).fetchone()
        return row is not None

    def start_submission(
        self, user_id: str, challenge_id: int, line_event_id: str | None, message_type: str
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO submissions (user_id, challenge_id, line_event_id, message_type, result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, challenge_id, line_event_id, message_type, "received", self._now()),
            )
        return int(cursor.lastrowid)

    def finish_submission(self, submission_id: int, result: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE submissions SET result = ? WHERE id = ?", (result, submission_id))

    def add_photo_submission(
        self, submission_id: int, line_message_id: str, local_path_or_url: str, ai_result: str | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO photo_submissions (submission_id, line_message_id, local_path_or_url, ai_result) VALUES (?, ?, ?, ?)",
                (submission_id, line_message_id, local_path_or_url, ai_result),
            )

    def photo_categories(self, user_id: str, challenge_id: int) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT photo_submissions.ai_result
                FROM photo_submissions
                JOIN submissions ON submissions.id = photo_submissions.submission_id
                WHERE submissions.user_id = ? AND submissions.challenge_id = ?
                """,
                (user_id, challenge_id),
            ).fetchall()
        return {str(row["ai_result"]) for row in rows if row["ai_result"]}


class LineClient:
    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None):
        self._access_token = access_token
        self._client = client or httpx.AsyncClient(timeout=30)

    def _auth_headers(self) -> dict[str, str]:
        return {"Author" + "ization": "Bearer " + self._access_token}

    async def get_content(self, message_id: str) -> tuple[bytes, str]:
        response = await self._client.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def get_profile(self, user_id: str) -> str:
        response = await self._client.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        profile: dict[str, Any] = response.json()
        return str(profile["displayName"])

    async def start_loading(self, chat_id: str, loading_seconds: int = 60) -> None:
        response = await self._client.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
            },
            json={"chatId": chat_id, "loadingSeconds": loading_seconds},
        )
        response.raise_for_status()

    async def link_rich_menu(self, user_id: str, rich_menu_id: str) -> None:
        response = await self._client.post(
            f"https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}",
            headers=self._auth_headers(),
        )
        response.raise_for_status()

    async def reply(self, reply_token: str, message: Message | list[Message]) -> None:
        messages = [message] if isinstance(message, (str, dict)) else message
        response = await self._client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [self._format_message(message) for message in messages[:5]],
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error("LINE reply failed (%s): %s", response.status_code, response.text)
            raise

    @staticmethod
    def _format_message(message: Message) -> dict[str, object]:
        if isinstance(message, str):
            return {"type": "text", "text": message[:5000]}
        return dict(message)


class GeminiClient:
    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=60)

    async def describe_image(self, image: bytes, mime_type: str) -> str:
        encoded_image = base64.b64encode(image).decode("ascii")
        response = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent",
            params={"key": self._api_key},
            json={
                "systemInstruction": {
                    "parts": [{"text": "The user was instructed to upload an photo. Please generate a description for the photo."}]
                },
                "contents": [{
                    "role": "user",
                    "parts": [{"inline_data": {"mime_type": mime_type, "data": encoded_image}}],
                }],
                "generationConfig": {"responseModalities": ["TEXT"]},
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def check_image(self, image: bytes, mime_type: str, instructions: str) -> bool:
        encoded_image = base64.b64encode(image).decode("ascii")
        response = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent",
            params={"key": self._api_key},
            json={
                "systemInstruction": {
                    "parts": [{
                        "text": (
                            "Decide whether the image satisfies this requirement: "
                            f"{instructions} Return YES only when the requirement is clearly met. "
                            "Return NO when the required subject is absent, obscured, too small, "
                            "or uncertain. Reply with exactly YES or NO."
                        )
                    }]
                },
                "contents": [{
                    "role": "user",
                    "parts": [{"inline_data": {"mime_type": mime_type, "data": encoded_image}}],
                }],
                "generationConfig": {"responseModalities": ["TEXT"]},
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        result = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        return result.startswith("YES")

    async def classify_image(
        self,
        image: bytes,
        mime_type: str,
        instructions: str,
        categories: tuple[str, ...],
        references: list[tuple[bytes, str]],
    ) -> str | None:
        parts: list[dict[str, Any]] = [{"text": (
            f"{instructions} Classify the user's image as exactly one of: "
            f"{', '.join(categories)}. Return UNKNOWN if none is clearly shown."
        )}, {"inline_data": {
            "mime_type": mime_type,
            "data": base64.b64encode(image).decode("ascii"),
        }}]
        for category, (reference, reference_mime_type) in zip(categories, references):
            parts.extend([
                {"text": f"Reference image for category: {category}"},
                {"inline_data": {
                    "mime_type": reference_mime_type,
                    "data": base64.b64encode(reference).decode("ascii"),
                }},
            ])
        response = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent",
            params={"key": self._api_key},
            json={
                "systemInstruction": {"parts": [{
                    "text": "Compare the user's photo with the reference images. Reply with exactly one category name or UNKNOWN."
                }]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["TEXT"]},
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        result = data["candidates"][0]["content"]["parts"][0]["text"].strip().casefold()
        return next((category for category in categories if result == category.casefold()), None)
