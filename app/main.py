import hmac
import hashlib
import base64
import logging
from typing import Any

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


def get_user_store() -> UserStore:
    global user_store
    if user_store is None:
        user_store = UserStore(settings.user_csv_path)
    return user_store


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
    for event in payload.get("events", []):
        await handle_event(event)
    return {"status": "ok"}


async def handle_event(event: dict[str, Any]) -> None:
    if event.get("type") != "message":
        return

    source = event.get("source", {})
    user_id = source.get("userId")
    reply_token = event.get("replyToken")
    if not user_id or not reply_token:
        logger.warning("Ignoring incomplete message event")
        return

    store = get_user_store()
    if not store.has_user(user_id):
        store.add_user(user_id)
        step = current_step(0)
        await line_client.reply(
            reply_token,
            message_list(step.instructions) if step else "The adventure is not configured yet.",
        )
        return

    step = current_step(store.get_step_index(user_id))
    if step is None:
        await line_client.reply(reply_token, "You completed the adventure. Thanks for playing!")
        return

    message = event.get("message", {})
    message_type = message.get("type")
    if step.kind == "text" and message_type == "text":
        if not answer_is_correct(step, message.get("text", "")):
            await line_client.reply(reply_token, ["Not quite. Try again.", *message_list(step.instructions)])
            return
        await advance_player(store, user_id, reply_token, step.success_message)
        return

    if step.kind == "location" and message_type == "location":
        distance = distance_meters(
            float(message["latitude"]), float(message["longitude"]), step.latitude or 0, step.longitude or 0
        )
        if distance > step.radius_meters:
            await line_client.reply(reply_token, f"You are about {round(distance)}m away. Move closer and send your location again.")
            return
        await advance_player(store, user_id, reply_token, step.success_message)
        return

    if step.kind == "photo" and message_type == "image":
        message_id = message.get("id")
        if not message_id:
            logger.warning("Ignoring image event without a message ID")
            return
        image, mime_type = await line_client.get_content(message_id)
        accepted = await gemini_client.check_image(image, mime_type, step.photo_check)
        if accepted:
            await advance_player(store, user_id, reply_token, step.success_message)
        else:
            await line_client.reply(
                reply_token,
                ["I could not verify that photo. Try again.", *message_list(step.instructions)],
            )
        return

    await line_client.reply(
        reply_token,
        [f"This turn needs a {step.kind} response.", *message_list(step.instructions)],
    )


async def advance_player(
    store: UserStore, user_id: str, reply_token: str, success_message: Message | list[Message]
) -> None:
    next_index = store.get_step_index(user_id) + 1
    store.set_step_index(user_id, next_index)
    next_step = current_step(next_index)
    message = message_list(success_message) or ["Correct."]
    if next_step:
        message.extend(message_list(next_step.instructions))
    await line_client.reply(reply_token, message)
