import base64
import csv
from pathlib import Path
from typing import Any

import httpx

from .game import Message


class UserStore:
    def __init__(self, csv_path: str):
        self._csv_path = Path(csv_path)
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._csv_path.exists():
            with self._csv_path.open("w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(["user_id", "state", "step_index"])
        else:
            self._migrate_legacy_csv()

    def _migrate_legacy_csv(self) -> None:
        with self._csv_path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            if reader.fieldnames == ["user_id", "state", "step_index"]:
                return
        with self._csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["user_id", "state", "step_index"])
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "user_id": row.get("user_id", ""),
                    "state": row.get("state", "new"),
                    "step_index": row.get("step_index") or "0",
                })

    def get_step_index(self, user_id: str) -> int:
        with self._csv_path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                if row.get("user_id") == user_id:
                    return int(row.get("step_index") or 0)
        return 0

    def set_step_index(self, user_id: str, step_index: int) -> None:
        with self._csv_path.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        for row in rows:
            if row.get("user_id") == user_id:
                row["step_index"] = str(step_index)
        with self._csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["user_id", "state", "step_index"])
            writer.writeheader()
            writer.writerows(rows)

    def has_user(self, user_id: str) -> bool:
        with self._csv_path.open(newline="", encoding="utf-8") as file:
            return any(row.get("user_id") == user_id for row in csv.DictReader(file))

    def add_user(self, user_id: str) -> None:
        with self._csv_path.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([user_id, "new", "0"])


class LineClient:
    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None):
        self._access_token = access_token
        self._client = client or httpx.AsyncClient(timeout=30)

    async def get_content(self, message_id: str) -> tuple[bytes, str]:
        response = await self._client.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def start_loading(self, chat_id: str, loading_seconds: int = 60) -> None:
        response = await self._client.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            json={"chatId": chat_id, "loadingSeconds": loading_seconds},
        )
        response.raise_for_status()

    async def reply(self, reply_token: str, message: Message | list[Message]) -> None:
        messages = [message] if isinstance(message, str) else message
        response = await self._client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [self._format_message(message) for message in messages[:5]],
            },
        )
        response.raise_for_status()

    @staticmethod
    def _format_message(message: Message) -> dict[str, str]:
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
