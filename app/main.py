import hmac
import hashlib
import base64
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from .config import Settings
from .game import Message, answer_is_correct, current_step, distance_meters, message_list
from .integrations import GeminiClient, LineClient, UserStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LINE Photo Bot")
settings = Settings.from_environment()
user_store: UserStore | None = None
line_client = LineClient(settings.line_channel_access_token)
gemini_client = GeminiClient(settings.gemini_api_key, settings.gemini_model)


def normalize_command(value: str) -> str:
    return re.sub(r"[\s，,。.!！?？:：；;、]+", "", value).casefold()


def get_user_store() -> UserStore:
    global user_store
    if user_store is None:
        user_store = UserStore(settings.database_path)
    return user_store


async def send_reply(
    store: UserStore, reply_token: str, user_id: str, message: Message | list[Message]
) -> None:
    formatted_messages = [message] if isinstance(message, str) else message
    store.record_line_event(None, user_id, "outbound", {
        "replyToken": reply_token,
        "messages": [LineClient._format_message(item) for item in formatted_messages[:5]],
    })
    try:
        await line_client.reply(reply_token, message)
    except httpx.HTTPStatusError:
        logger.exception("Could not send LINE reply for user %s", user_id)


def verify_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(
        settings.line_channel_secret.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str | None = Header(default=None)) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    payload: dict[str, Any] = await request.json()
    get_user_store().record_line_event(None, None, "inbound", payload)
    for event in payload.get("events", []):
        await handle_event(event)
    return {"status": "ok"}


async def handle_event(event: dict[str, Any]) -> None:
    if event.get("type") != "message":
        return

    source = event.get("source", {})
    user_id = source.get("userId")
    store = get_user_store()
    line_event_id = event.get("webhookEventId")
    if store.has_line_event(line_event_id):
        logger.info("Ignoring duplicate LINE event %s", line_event_id)
        return
    store.record_line_event(line_event_id, user_id, "inbound", event)
    reply_token = event.get("replyToken")
    if not user_id or not reply_token:
        logger.warning("Ignoring incomplete message event")
        return

    if not store.has_user(user_id):
        store.add_user(user_id)
        step = current_step(0)
        await send_reply(
            store, reply_token, user_id,
            message_list(step.instructions) if step else "The adventure is not configured yet.",
        )
        return

    message = event.get("message", {})
    message_type = message.get("type")
    command = normalize_command(message.get("text", "")) if message_type == "text" else ""
    if command in {"開始", "開始吧", "start", "重新開始", "重來", "restart"}:
        store.reset_user(user_id)
        step = current_step(0)
        await send_reply(
            store, reply_token, user_id,
            ["新的旅程開始了。", *(message_list(step.instructions) if step else [])],
        )
        return

    step = current_step(store.get_step_index(user_id))
    if step is None:
        await send_reply(store, reply_token, user_id, "You completed the adventure. Thanks for playing!")
        return

    if command in {"進度", "目前進度", "progress"}:
        await send_reply(
            store, reply_token, user_id,
            f"目前進度：{step.title}\n完成後即可繼續下一段故事。",
        )
        return

    if step.kind == "text" and message_type == "text":
        submission_id = store.start_submission(user_id, store.get_step_index(user_id), event.get("webhookEventId"), message_type)
        if not answer_is_correct(step, message.get("text", "")):
            store.finish_submission(submission_id, "incorrect")
            await send_reply(store, reply_token, user_id, ["Not quite. Try again.", *message_list(step.instructions)])
            return
        store.finish_submission(submission_id, "accepted")
        await advance_player(store, user_id, reply_token, step.success_message)
        return

    if step.kind == "location" and message_type == "location":
        try:
            distance = distance_meters(
                float(message["latitude"]),
                float(message["longitude"]),
                step.latitude or 0,
                step.longitude or 0,
            )
        except (KeyError, TypeError, ValueError):
            await send_reply(store, reply_token, user_id, "我讀不到這個位置，請重新分享目前位置。")
            return
        if distance > step.radius_meters:
            await send_reply(store, reply_token, user_id, f"You are about {round(distance)}m away. Move closer and send your location again.")
            return
        await advance_player(store, user_id, reply_token, step.success_message)
        return

    if step.kind == "photo" and message_type == "image":
        message_id = message.get("id")
        if not message_id:
            logger.warning("Ignoring image event without a message ID")
            return
        submission_id = store.start_submission(user_id, store.get_step_index(user_id), event.get("webhookEventId"), message_type)
        if source.get("type") == "user":
            await line_client.start_loading(user_id)
        try:
            image, mime_type = await line_client.get_content(message_id)
            accepted = await gemini_client.check_image(image, mime_type, step.photo_check)
            media_directory = Path(settings.media_path)
            media_directory.mkdir(parents=True, exist_ok=True)
            media_path = media_directory / f"{message_id}"
            media_path.write_bytes(image)
            store.add_photo_submission(submission_id, message_id, str(media_path), "accepted" if accepted else "rejected")
            store.finish_submission(submission_id, "accepted" if accepted else "rejected")
        except Exception:
            store.finish_submission(submission_id, "error")
            logger.exception("Photo verification failed for user %s", user_id)
            await send_reply(
                store, reply_token, user_id,
                ["照片檢查暫時失敗，請稍後再試。", *message_list(step.instructions)],
            )
            return
        if accepted:
            await advance_player(store, user_id, reply_token, step.success_message)
        else:
            await send_reply(
                store, reply_token, user_id,
                ["I could not verify that photo. Try again.", *message_list(step.instructions)],
            )
        return

    await send_reply(
        store, reply_token, user_id,
        [f"This turn needs a {step.kind} response.", *message_list(step.instructions)],
    )


async def advance_player(
    store: UserStore, user_id: str, reply_token: str, success_message: Message | list[Message]
) -> None:
    next_index = store.get_step_index(user_id) + 1
    next_step = current_step(next_index)
    message = message_list(success_message)
    if next_step:
        message.extend(message_list(next_step.instructions))
    store.set_step_index(user_id, next_index)
    if next_step is None:
        store.mark_completed(user_id)
    if message:
        await send_reply(store, reply_token, user_id, message)
