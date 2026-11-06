import hmac
import hashlib
import base64
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .game import GAME_STEPS, GameStep, Message, answer_is_correct, current_step, distance_meters, map_message, message_list, route_hint_for_target, story_audio_message
from .integrations import GeminiClient, LineClient, UserStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LINE Photo Bot")
app.mount(
    "/assets/voice",
    StaticFiles(directory=Path(__file__).resolve().parent.parent / "voice"),
    name="asset-voice",
)
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).resolve().parent.parent / "assets"),
    name="assets",
)
settings = Settings.from_environment()
user_store: UserStore | None = None
line_client = LineClient(settings.line_channel_access_token)
gemini_client = GeminiClient(settings.gemini_api_key, settings.gemini_model)

PHOTO_CATEGORY_LABELS = {
    "carved pillar": "百鳥朝梅柱",
    "bell": "鐘",
    "temple exterior": "祖師廟外觀",
}
STEP_KIND_LABELS = {
    "text": "文字",
    "photo": "照片",
    "location": "位置",
}
MAP_COMMANDS = {"地圖", "三峽觀光地圖"}
RESTART_COMMANDS = {"開始", "開始吧", "start", "重新開始", "重來", "restart"}
KNOWLEDGE_MENU_COMMANDS = {"科普資料", "知識選單"}
MAIN_MENU_COMMANDS = {"返回主選單", "主選單"}
INPUT_COMMANDS = {"輸入答案"}
CURRENT_CHALLENGE_COMMANDS = {"目前任務", "目前挑戰", "重看任務"}
INTRO_COMMANDS = {"遊戲說明", "初始說明"}
HINT_COMMANDS = {"提示", "查看提示"}
CONTINUE_COMMANDS = {"請繼續"}
SILENT_STORY_CHUNKS = {(2, 3)}
INITIAL_INSTRUCTIONS = """🎒 【出發前，請確認你的裝備】
1. 實體解謎道具包（內含各關卡信封、照片、拼圖）
2. 三峽區觀光導覽地圖（可於遊客服務中心索取）
3. 一支電量充足的手機

🕹️ 【遊戲怎麼玩？】
• 跟隨對話指引，實地走訪三峽各個歷史景點（建議從「三峽祖師廟」周邊出發）。
• 根據劇情提示打開對應的關卡信封，結合「實體道具」與「現場環境」進行解謎。
• 在這個聊天室中「輸入答案」或「上傳指定照片」，正確即可推進劇情！
"""
WELCOME_STORY = """喀嚓！📸 歡迎來到《藍不住了！尋找藍的祕密》！

喜歡帶著拍立得到處旅行的女孩「勾玉」，有次旅行到三峽老街在拍古井時，突然被刺眼的白光籠罩……
醒來後，她發現自己竟然穿回過去的「三角湧」"""
WELCOME_START_PROMPT = "準備好跨越時空、找回三峽記憶中的那抹藍了嗎？"
GROUP_WELCOME = (
    "大家好！我是《藍不住了！尋找藍的祕密》解謎機器人。\n"
    "群組中的每位玩家都會各自保存遊戲進度，請直接輸入「開始吧!」開始遊戲。"
)
POSTBACK_COMMANDS = {
    "input_answer": "輸入答案",
    "map": "三峽觀光地圖",
    "current_challenge": "目前任務",
    "game_instructions": "遊戲說明",
    "hint": "提示",
    "restart": "重新開始",
}


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
    formatted_messages = [message] if isinstance(message, (str, dict)) else message
    store.record_line_event(None, user_id, "outbound", {
        "replyToken": reply_token,
        "messages": [LineClient._format_message(item) for item in formatted_messages[:5]],
    })
    try:
        await line_client.reply(reply_token, message)
    except httpx.HTTPStatusError:
        logger.exception("Could not send LINE reply for user %s", user_id)


def step_messages(step: GameStep, story_index: int = 0) -> list[Message]:
    source_messages = message_list(step.instructions)
    challenge = next(message for message in reversed(source_messages) if isinstance(message, str))
    media = [message for message in source_messages if isinstance(message, dict)]
    if story_index < len(step.story_chunks):
        story = step.story_chunks[story_index]
        target_match = re.search(r"🌟\s*([^，。！？；：、)）\n]+)", story)
        target = re.sub(r"[「」『』\"“”]", "", target_match.group(1)).strip() if target_match else ""
        story_message_payload = story_message(
            story, True, target if route_hint_for_target(target) else ""
        )
        messages = [story_message_payload]
        audio_index = story_audio_index(step, story_index)
        if audio_index is not None:
            audio_message = story_audio_message(audio_index)
            if isinstance(story_message_payload, dict) and "quickReply" in story_message_payload:
                audio_message["quickReply"] = story_message_payload["quickReply"]
            messages.append(audio_message)
        return messages
    messages = [*media, challenge]
    if not isinstance(challenge, str):
        raise ValueError(f"Challenge text missing for step {step.title!r}")
    if not step.quick_reply_options and not step.quick_reply_location:
        return messages
    quick_reply_items = [
        {"type": "action", "action": {"type": "message", "label": option, "text": option}}
        for option in step.quick_reply_options
    ]
    if step.quick_reply_location:
        quick_reply_items.append(
            {"type": "action", "action": {"type": "location", "label": "分享目前位置"}}
        )
    messages[-1] = {
        "type": "text",
        "text": challenge,
        "quickReply": {
            "items": quick_reply_items
        },
    }
    return messages


def story_message(text: str, has_next: bool, target: str = "") -> Message:
    message: dict[str, Any] = {"type": "text", "text": text}
    if has_next:
        items = [{"type": "action", "action": {"type": "message", "label": "請繼續", "text": "請繼續"}}]
        if target:
            items.append(
                {"type": "action", "action": {"type": "message", "label": f"{target}怎麼去？", "text": f"{target}怎麼去？"}}
            )
        message["quickReply"] = {
            "items": items
        }
    return message


def story_audio_index(step: GameStep, story_index: int) -> int | None:
    step_index = GAME_STEPS.index(step)
    if (step_index, story_index) in SILENT_STORY_CHUNKS:
        return None
    previous_audio_count = sum(
        len(game_step.story_chunks) for game_step in GAME_STEPS[:step_index]
    ) - sum(
        1 for silent_step, _ in SILENT_STORY_CHUNKS if silent_step < step_index
    )
    current_audio_count = sum(
        1
        for previous_story_index in range(story_index)
        if (step_index, previous_story_index) not in SILENT_STORY_CHUNKS
    )
    return previous_audio_count + current_audio_count


def story_reply(text: str, has_next: bool, audio_index: int, target: str = "") -> list[Message]:
    story_message_payload = story_message(text, has_next, target)
    audio_message = story_audio_message(audio_index)
    if isinstance(story_message_payload, dict) and "quickReply" in story_message_payload:
        audio_message["quickReply"] = story_message_payload["quickReply"]
    return [story_message_payload, audio_message]


def route_hint_message(text: str, target: str) -> Message:
    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {"type": "action", "action": {"type": "message", "label": "請繼續", "text": "請繼續"}},
                {"type": "action", "action": {"type": "message", "label": f"{target}怎麼去？", "text": f"{target}怎麼去？"}},
            ]
        },
    }


def location_retry_message(text: str) -> Message:
    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {"type": "action", "action": {"type": "location", "label": "分享目前位置"}}
            ]
        },
    }


def incorrect_answer_message(step: GameStep) -> Message:
    challenge_message = step_messages(step)[-1]
    if isinstance(challenge_message, dict) and "quickReply" in challenge_message:
        return {
            "type": "text",
            "text": "不太對喔！再想想看！",
            "quickReply": challenge_message["quickReply"],
        }
    return "不太對喔！再想想看！"


def welcome_messages(nickname: str) -> list[Message]:
    return [
        f"{nickname}您好！\n{WELCOME_STORY}",
        INITIAL_INSTRUCTIONS,
        {
            "type": "text",
            "text": WELCOME_START_PROMPT,
            "quickReply": {
                "items": [
                    {
                        "type": "action",
                        "action": {
                            "type": "message",
                            "label": "開始吧!",
                            "text": "開始吧!",
                        },
                    }
                ]
            },
        },
    ]


async def get_nickname(user_id: str) -> str:
    try:
        return await line_client.get_profile(user_id)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        logger.exception("Could not retrieve LINE profile for user %s", user_id)
        return "玩家"


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
    event_type = event.get("type")
    if event_type not in {"message", "postback", "follow", "join"}:
        return

    source = event.get("source", {})
    user_id = source.get("userId")
    event_actor_id = user_id or source.get("groupId") or source.get("roomId")
    store = get_user_store()
    line_event_id = event.get("webhookEventId")
    if store.has_line_event(line_event_id):
        logger.info("Ignoring duplicate LINE event %s", line_event_id)
        return
    store.record_line_event(line_event_id, event_actor_id, "inbound", event)
    reply_token = event.get("replyToken")
    if not event_actor_id or not reply_token:
        logger.warning("Ignoring incomplete %s event", event_type)
        return

    if event_type == "join":
        await send_reply(store, reply_token, event_actor_id, GROUP_WELCOME)
        return

    if event_type == "follow":
        if not user_id:
            logger.warning("Ignoring follow event without a user ID")
            return
        if not store.has_user(user_id):
            store.add_user(user_id)
        await send_reply(
            store, reply_token, user_id, welcome_messages(await get_nickname(user_id))
        )
        return

    message = event.get("message", {})
    message_type = message.get("type")
    postback_data = event.get("postback", {}).get("data", "")
    if event.get("type") == "postback":
        if postback_data in {"switch_to_main", "switch_to_knowledge"}:
            return
        if postback_data == "input_answer":
            return
        command = normalize_command(POSTBACK_COMMANDS.get(postback_data, ""))
    else:
        command = normalize_command(message.get("text", "")) if message_type == "text" else ""

    if command.endswith("怎麼去"):
        target = command.removesuffix("怎麼去")
        hint_number = store.increment_route_hint_count(user_id) if store.has_user(user_id) else 1
        route_hint = route_hint_for_target(target, hint_number)
        if route_hint:
            if not store.has_user(user_id):
                store.add_user(user_id)
            await send_reply(store, reply_token, user_id, route_hint_message(route_hint, target))
            return

    if command in MAP_COMMANDS:
        if not store.has_user(user_id):
            store.add_user(user_id)
        await send_reply(store, reply_token, user_id, map_message())
        return

    if command in KNOWLEDGE_MENU_COMMANDS:
        if not settings.knowledge_rich_menu_id:
            logger.error("KNOWLEDGE_RICH_MENU_ID is not configured")
            await send_reply(store, reply_token, user_id, "科普資料選單目前尚未設定，請稍後再試。")
            return
        await line_client.link_rich_menu(user_id, settings.knowledge_rich_menu_id)
        await send_reply(store, reply_token, user_id, "已開啟科普資料選單。")
        return

    if command in MAIN_MENU_COMMANDS:
        if not settings.main_rich_menu_id:
            logger.error("MAIN_RICH_MENU_ID is not configured")
            await send_reply(store, reply_token, user_id, "主選單目前尚未設定，請稍後再試。")
            return
        await line_client.link_rich_menu(user_id, settings.main_rich_menu_id)
        await send_reply(store, reply_token, user_id, "已返回主選單。")
        return

    if not store.has_user(user_id) and (
        command in RESTART_COMMANDS
        or command in INPUT_COMMANDS
        or command in CURRENT_CHALLENGE_COMMANDS
        or command in INTRO_COMMANDS
        or command in HINT_COMMANDS
        or command in CONTINUE_COMMANDS
    ):
        store.add_user(user_id)

    if command in RESTART_COMMANDS:
        store.reset_user(user_id)
        step = current_step(0)
        await send_reply(
            store, reply_token, user_id,
            ["新的旅程開始了。", *(step_messages(step, store.get_story_index(user_id)) if step else [])],
        )
        return

    if command in INTRO_COMMANDS:
        await send_reply(store, reply_token, user_id, INITIAL_INSTRUCTIONS)
        return

    if command in INPUT_COMMANDS:
        await send_reply(store, reply_token, user_id, "請在聊天室下方輸入答案，或依照關卡要求上傳照片。")
        return

    if command in CURRENT_CHALLENGE_COMMANDS:
        step = current_step(store.get_step_index(user_id))
        await send_reply(
            store,
            reply_token,
            user_id,
            step_messages(step, store.get_story_index(user_id)) if step else "旅程已完成，謝謝你的參與！",
        )
        return

    if command in HINT_COMMANDS:
        step = current_step(store.get_step_index(user_id))
        if step is None:
            await send_reply(store, reply_token, user_id, "旅程已完成，沒有需要提示的關卡。")
        else:
            story_index = store.get_story_index(user_id)
            if story_index < len(step.story_chunks):
                story = step.story_chunks[story_index]
                target_match = re.search(r"🌟\s*([^，。！？；：、)）\n]+)", story)
                target = re.sub(r"[「」『』\"“”]", "", target_match.group(1)).strip() if target_match else ""
                route_hint = route_hint_for_target(target)
                if route_hint:
                    await send_reply(store, reply_token, user_id, route_hint_message(route_hint, target))
                    return
            hint_number = store.increment_hint_count(user_id)
            hints = step.hints or ((step.hint, ""),)
            hint = hints[min(hint_number - 1, len(hints) - 1)]
            current_messages = step_messages(step, store.get_story_index(user_id))
            follow_up = next(
                (
                    message
                    for message in reversed(current_messages)
                    if isinstance(message, dict) and "quickReply" in message
                ),
                None,
            )
            hint_text = hint or "本關提示正在準備中，請先依照目前任務中的線索探索看看！"
            if isinstance(follow_up, dict) and "quickReply" in follow_up:
                hint_message: Message = {
                    "type": "text",
                    "text": hint_text,
                    "quickReply": follow_up["quickReply"],
                }
            else:
                hint_message = hint_text
            await send_reply(
                store,
                reply_token,
                user_id,
                hint_message,
            )
        return

    if not store.has_user(user_id):
        store.add_user(user_id)
        await send_reply(
            store, reply_token, user_id, welcome_messages(await get_nickname(user_id)),
        )
        return

    step = current_step(store.get_step_index(user_id))
    if step is None:
        if store.get_step_index(user_id) == len(GAME_STEPS):
            ending_chunks = GAME_STEPS[-1].ending_chunks
            story_index = store.get_story_index(user_id)
            if story_index < len(ending_chunks):
                store.set_story_index(user_id, story_index + 1)
                if story_index + 1 == len(ending_chunks):
                    store.mark_completed(user_id)
                await send_reply(
                    store, reply_token, user_id,
                    story_reply(
                        ending_chunks[story_index],
                        story_index + 1 < len(ending_chunks),
                        sum(len(game_step.story_chunks) for game_step in GAME_STEPS)
                        - len(SILENT_STORY_CHUNKS)
                        + story_index,
                    ),
                )
                return
        await send_reply(store, reply_token, user_id, "旅程已完成，謝謝你的參與！")
        return

    if command in CONTINUE_COMMANDS:
        story_index = store.get_story_index(user_id)
        if story_index < len(step.story_chunks):
            store.set_story_index(user_id, story_index + 1)
        await send_reply(
            store,
            reply_token,
            user_id,
            step_messages(step, store.get_story_index(user_id)),
        )
        return

    if command in {"進度", "目前進度", "progress"}:
        await send_reply(
            store, reply_token, user_id,
            f"目前進度：{step.title}\n完成後即可繼續下一段故事。",
        )
        return

    if step.story_chunks and store.get_story_index(user_id) < len(step.story_chunks):
        await send_reply(
            store,
            reply_token,
            user_id,
            step_messages(step, store.get_story_index(user_id)),
        )
        return

    if step.kind == "text" and message_type == "text":
        submission_id = store.start_submission(user_id, store.get_step_index(user_id), event.get("webhookEventId"), message_type)
        if not answer_is_correct(step, message.get("text", "")):
            store.finish_submission(submission_id, "incorrect")
            await send_reply(store, reply_token, user_id, incorrect_answer_message(step))
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
            await send_reply(
                store,
                reply_token,
                user_id,
                location_retry_message("我讀不到這個位置，請重新分享目前位置。"),
            )
            return
        if distance > step.radius_meters:
            await send_reply(
                store,
                reply_token,
                user_id,
                location_retry_message(
                    f"你目前距離目標約 {round(distance)} 公尺，請再靠近一些後重新分享目前位置。"
                ),
            )
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
            matched_category: str | None = None
            if step.photo_categories:
                references = []
                for reference_name in step.photo_references:
                    reference_path = Path(__file__).resolve().parent.parent / "assets" / reference_name
                    references.append((reference_path.read_bytes(), "image/jpeg"))
                matched_category = await gemini_client.classify_image(
                    image, mime_type, step.photo_check, step.photo_categories, references
                )
                accepted = matched_category is not None
            else:
                accepted = await gemini_client.check_image(image, mime_type, step.photo_check)
            media_directory = Path(settings.media_path)
            media_directory.mkdir(parents=True, exist_ok=True)
            media_path = media_directory / f"{message_id}"
            media_path.write_bytes(image)
            result = matched_category or ("accepted" if accepted else "rejected")
            store.add_photo_submission(submission_id, message_id, str(media_path), result)
            store.finish_submission(submission_id, result if accepted else "rejected")
        except Exception:
            store.finish_submission(submission_id, "error")
            logger.exception("Photo verification failed for user %s", user_id)
            await send_reply(
                store, reply_token, user_id,
                "照片檢查暫時失敗，請稍後再試。",
            )
            return
        if accepted:
            if step.photo_categories:
                completed_categories = store.photo_categories(
                    user_id, store.get_step_index(user_id)
                )
                remaining = [
                    category
                    for category in step.photo_categories
                    if category not in completed_categories
                ]
                if remaining:
                    await send_reply(
                        store,
                        reply_token,
                        user_id,
                        "收到，還需要："
                        f"{'、'.join(PHOTO_CATEGORY_LABELS.get(category, category) for category in remaining)}。",
                    )
                else:
                    await advance_player(store, user_id, reply_token, step.success_message)
            else:
                await advance_player(store, user_id, reply_token, step.success_message)
        else:
            await send_reply(
                store, reply_token, user_id,
                "我無法確認這張照片，請再試一次。",
            )
        return

    await send_reply(
        store, reply_token, user_id,
        [f"這一關需要提供{STEP_KIND_LABELS[step.kind]}回應。", *step_messages(step)],
    )


async def advance_player(
    store: UserStore, user_id: str, reply_token: str, success_message: Message | list[Message]
) -> None:
    next_index = store.get_step_index(user_id) + 1
    next_step = current_step(next_index)
    ending_chunks = GAME_STEPS[-1].ending_chunks if next_step is None else ()
    if ending_chunks:
        message = [
            "沒錯！正確答案！",
            *story_reply(
                ending_chunks[0],
                len(ending_chunks) > 1,
                sum(len(game_step.story_chunks) for game_step in GAME_STEPS)
                - len(SILENT_STORY_CHUNKS),
            ),
        ]
    else:
        message = ["沒錯！正確答案！", *message_list(success_message)]
    if next_step:
        message.extend(step_messages(next_step, 0))
    store.set_step_index(user_id, next_index)
    if ending_chunks:
        store.set_story_index(user_id, 1)
    elif next_step is None:
        store.mark_completed(user_id)
    if message:
        await send_reply(store, reply_token, user_id, message)
