import base64
import hashlib
import hmac
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.game import (
    GAME_STEPS,
    GameStep,
    answer_is_correct,
    audio_asset_message,
    distance_meters,
    map_message,
    message_list,
)
from app.integrations import LineClient, UserStore
from app.setup_rich_menus import _postback_action


def signature(body: bytes) -> str:
    digest = hmac.new(main.settings.line_channel_secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_health() -> None:
    assert TestClient(main.app).get("/health").json() == {"status": "ok"}


def test_webhook_rejects_invalid_signature() -> None:
    response = TestClient(main.app).post("/webhook", content=b"{}", headers={"x-line-signature": "bad"})
    assert response.status_code == 400


def test_non_image_event_is_ignored() -> None:
    body = json.dumps({"events": [{"type": "message", "message": {"type": "text"}}]}).encode()
    response = TestClient(main.app).post("/webhook", content=body, headers={"x-line-signature": signature(body)})
    assert response.status_code == 200


def test_user_store_uses_local_sqlite_database(tmp_path) -> None:
    database_path = tmp_path / "bot.db"
    store = UserStore(str(database_path))

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"players", "submissions", "photo_submissions", "line_events"} <= tables
    assert not store.has_user("user-1")

    store.add_user("user-1")

    assert store.has_user("user-1")
    assert store.get_step_index("user-1") == 0

    store.reset_all()
    assert store.get_step_index("user-1") == 0


def test_reset_user_clears_previous_photo_categories(tmp_path) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    store.add_user("user-1")

    submission_ids = [
        store.start_submission("user-1", 8, f"event-{index}", "image")
        for index in range(3)
    ]
    for submission_id, category in zip(
        submission_ids, ("carved pillar", "bell", "temple exterior")
    ):
        store.add_photo_submission(submission_id, f"message-{submission_id}", "/tmp/photo", category)

    assert store.photo_categories("user-1", 8) == {
        "carved pillar", "bell", "temple exterior"
    }

    store.reset_user("user-1")

    assert store.photo_categories("user-1", 8) == set()


def test_text_answers_are_case_insensitive_and_support_alternatives() -> None:
    step = GameStep(title="Clue", instructions="Answer", kind="text", answer="north, N")

    assert answer_is_correct(step, " NORTH ")
    assert answer_is_correct(step, "n")
    assert not answer_is_correct(step, "south")


def test_commands_ignore_chat_punctuation() -> None:
    assert main.normalize_command("開始吧！") == "開始吧"
    assert main.normalize_command("  PROGRESS? ") == "progress"
    assert main.normalize_command("地圖") in main.MAP_COMMANDS
    assert main.normalize_command("重新開始") in main.RESTART_COMMANDS
    assert main.normalize_command("科普資料") in main.KNOWLEDGE_MENU_COMMANDS
    assert main.normalize_command("返回主選單") in main.MAIN_MENU_COMMANDS
    assert main.normalize_command("輸入答案") in main.INPUT_COMMANDS
    assert main.normalize_command("目前任務") in main.CURRENT_CHALLENGE_COMMANDS
    assert main.normalize_command("遊戲說明") in main.INTRO_COMMANDS
    assert main.normalize_command("提示") in main.HINT_COMMANDS


def test_initial_instructions_are_available_for_rich_menu_replay() -> None:
    assert "🎒 【出發前，請確認你的裝備】" in main.INITIAL_INSTRUCTIONS
    assert "🕹️ 【遊戲怎麼玩？】" in main.INITIAL_INSTRUCTIONS
    assert "輸入答案" in main.INITIAL_INSTRUCTIONS


async def test_follow_event_sends_welcome_without_resetting_progress(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    store.add_user("user-id")
    store.set_step_index("user-id", 3)
    replies = []

    async def capture_profile(user_id):
        assert user_id == "user-id"
        return "小明"

    async def capture_reply(reply_token, message):
        replies.append((reply_token, message))

    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main.line_client, "get_profile", capture_profile)
    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    await main.handle_event({
        "type": "follow",
        "webhookEventId": "follow-event",
        "replyToken": "reply-token",
        "source": {"userId": "user-id"},
    })

    assert store.get_step_index("user-id") == 3
    assert replies == [("reply-token", main.welcome_messages("小明"))]


async def test_join_event_welcomes_group_without_creating_group_player(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    replies = []

    async def capture_reply(reply_token, message):
        replies.append((reply_token, message))

    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    await main.handle_event({
        "type": "join",
        "webhookEventId": "join-event",
        "replyToken": "reply-token",
        "source": {"type": "group", "groupId": "group-id"},
    })

    assert replies == [("reply-token", main.GROUP_WELCOME)]
    assert not store.has_user("group-id")


async def test_route_question_returns_route_hint_instead_of_resending_story(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    store.add_user("user-id")
    store.set_step_index("user-id", 2)
    store.set_story_index("user-id", 3)
    replies = []

    async def capture_reply(reply_token, message):
        replies.append((reply_token, message))

    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    await main.handle_event({
        "type": "message",
        "webhookEventId": "route-event",
        "replyToken": "reply-token",
        "source": {"userId": "user-id"},
        "message": {"type": "text", "text": "藍染公園怎麼去？"},
    })

    assert replies == [
        (
            "reply-token",
            {
                "type": "text",
                "text": "請查閱藍染地圖之圖示四與五，並根據圖示方向前進。",
                "quickReply": {
                    "items": [
                        {"type": "action", "action": {"type": "message", "label": "請繼續", "text": "請繼續"}},
                        {"type": "action", "action": {"type": "message", "label": "藍染公園怎麼去？", "text": "藍染公園怎麼去？"}},
                    ]
                },
            },
        )
    ]


async def test_route_question_can_return_second_hint(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    store.add_user("user-id")
    store.set_step_index("user-id", 3)
    store.set_story_index("user-id", 0)
    replies = []

    async def capture_reply(reply_token, message):
        replies.append(message)

    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    event = {
        "type": "message",
        "replyToken": "reply-token",
        "source": {"userId": "user-id"},
        "message": {"type": "text", "text": "青出於染工作坊怎麼去？"},
    }
    await main.handle_event({**event, "webhookEventId": "route-first-hint"})
    await main.handle_event({
        **event,
        "webhookEventId": "route-second-hint",
        "message": {"type": "text", "text": "青出於染工作坊怎麼去？"},
    })

    assert replies[0]["text"] == "請查閱藍染地圖之圖示六，並根據圖示給予的線索，找到染坊。"
    assert replies[1]["text"] == "請查閱藍染地圖之圖示六，並抬頭尋找地圖給予之線索提示。"


async def test_challenge_ten_hint_starts_with_route_hint(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "bot.db"))
    store.add_user("user-id")
    store.set_step_index("user-id", 9)
    store.set_story_index("user-id", len(GAME_STEPS[9].story_chunks))
    replies = []

    async def capture_reply(reply_token, message):
        replies.append(message)

    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    await main.handle_event({
        "type": "message",
        "webhookEventId": "challenge-ten-route-hint",
        "replyToken": "reply-token",
        "source": {"userId": "user-id"},
        "message": {"type": "text", "text": "祖師廟怎麼去"},
    })
    await main.handle_event({
        "type": "message",
        "webhookEventId": "challenge-ten-hint",
        "replyToken": "reply-token",
        "source": {"userId": "user-id"},
        "message": {"type": "text", "text": "提示"},
    })

    assert replies[1]["text"] == "照片分別為外觀一張、內部一樓一張、二樓一張"
    assert replies[1]["quickReply"]["items"] == [
        {
            "type": "action",
            "action": {"type": "camera", "label": "開啟相機"},
        }
    ]
def test_story_without_route_hint_only_offers_continue() -> None:
    messages = main.step_messages(GAME_STEPS[6], story_index=0)
    message = messages[0]

    assert [item["action"]["text"] for item in message["quickReply"]["items"]] == ["請繼續"]
    assert messages[1]["quickReply"] == message["quickReply"]


def test_rich_menu_postbacks_are_silent_and_can_open_keyboard() -> None:
    assert _postback_action("map") == {
        "type": "postback",
        "data": "map",
    }
    assert _postback_action("input_answer", open_keyboard=True) == {
        "type": "postback",
        "data": "input_answer",
        "inputOption": "openKeyboard",
    }


def test_rich_menu_postback_data_maps_to_commands() -> None:
    assert main.POSTBACK_COMMANDS == {
        "input_answer": "輸入答案",
        "map": "三峽觀光地圖",
        "current_challenge": "目前任務",
        "game_instructions": "遊戲說明",
        "hint": "提示",
        "restart": "重新開始",
    }




def test_challenge_media_is_sent_with_challenge_not_story() -> None:
    chapter_seven = GAME_STEPS[9]

    story_messages = main.step_messages(chapter_seven, 0)
    challenge_messages = main.step_messages(chapter_seven, len(chapter_seven.story_chunks))

    assert not any(message.get("type") == "image" for message in story_messages if isinstance(message, dict))
    assert sum(message.get("type") == "image" for message in challenge_messages if isinstance(message, dict)) == 1


def test_photo_challenges_offer_to_open_camera() -> None:
    for step in (GAME_STEPS[3], GAME_STEPS[9]):
        challenge = main.step_messages(step, len(step.story_chunks))[-1]

        assert isinstance(challenge, dict)
        assert challenge["quickReply"]["items"][-1] == {
            "type": "action",
            "action": {"type": "camera", "label": "開啟相機"},
        }


def test_incorrect_quick_reply_answer_preserves_quick_replies() -> None:
    challenge = main.step_messages(GAME_STEPS[4])[-1]
    retry = main.incorrect_answer_message(GAME_STEPS[4])

    assert isinstance(challenge, dict)
    assert isinstance(retry, dict)
    assert retry["text"] == "不太對喔！再想想看！"
    assert retry["quickReply"] == challenge["quickReply"]


def test_final_photo_challenge_has_traditional_chinese_labels() -> None:
    assert main.PHOTO_CATEGORY_LABELS == {
        "carved pillar": "百鳥朝梅柱",
        "bell": "鐘",
        "temple exterior": "祖師廟外觀",
    }


def test_player_messages_exclude_facilitator_only_lines() -> None:
    forbidden_prefixes = ("目標地點：", "答案：", "答案:", "關鍵字：", "關鍵字:")
    player_messages = [
        message
        for step in GAME_STEPS
        for content in (step.instructions, step.success_message)
        for message in message_list(content)
        if isinstance(message, str)
    ]

    assert all(
        not line.startswith(forbidden_prefixes)
        for message in player_messages
        for line in message.splitlines()
    )


def test_player_messages_do_not_use_narrator_label() -> None:
    player_messages = [
        message
        for step in GAME_STEPS
        for content in (step.instructions, step.success_message)
        for message in message_list(content)
        if isinstance(message, str)
    ]

    assert all("旁白" not in message for message in player_messages)


def test_story_chunks_remove_facilitator_scaffolding_and_colon_prefixes() -> None:
    story = "\n".join(
        chunk
        for step in GAME_STEPS
        for chunk in step.story_chunks
    )

    assert "正確答案：" not in story
    assert "🔔提示" not in story
    assert not any(
        line.strip() in {":", "："} or line.lstrip().startswith((":", "："))
        for line in story.splitlines()
    )
    assert "勾玉走進三峽老街" in story
    assert "婆婆露出詭異的笑容" in story
    assert "路線提示" not in story


def test_route_story_message_has_continue_and_target_quick_replies() -> None:
    messages = main.step_messages(GAME_STEPS[2], story_index=3)
    message = messages[0]

    assert message["quickReply"]["items"] == [
        {"type": "action", "action": {"type": "message", "label": "請繼續", "text": "請繼續"}},
        {"type": "action", "action": {"type": "message", "label": "藍染公園怎麼去？", "text": "藍染公園怎麼去？"}},
    ]


def test_story_chunks_preserve_paragraph_spacing() -> None:
    first_story = GAME_STEPS[0].story_chunks[0]

    assert "三峽老街前進。\n\n途中，牛角麵包香氣" in first_story


def test_second_challenge_keeps_story_after_answer_before_chapter_two() -> None:
    story_chunks = GAME_STEPS[2].story_chunks
    story = "\n".join(story_chunks)

    assert len(story_chunks) > 1
    assert story_chunks[0].startswith("勾玉： 原來這塊石頭和藍染有關")
    assert story.index("原來這塊石頭和藍染有關") < story.index("染坊？")
    assert story.index("染坊？") < story.index("走出小巷後")


def test_each_chapter_sends_one_storyline_message() -> None:
    for step in GAME_STEPS:
        storyline_messages = [
            message
            for message in main.step_messages(step)
            if isinstance(message, str)
        ]

        assert len(storyline_messages) <= 2
        if len(storyline_messages) == 2:
            assert storyline_messages[0] != storyline_messages[1]


def test_location_distance_is_in_meters() -> None:
    assert distance_meters(0, 0, 0, 0.001) == pytest.approx(111.2, rel=0.01)


def test_game_steps_can_send_multiple_messages() -> None:
    step = GameStep(
        title="Clue",
        instructions=["The story begins.", "Solve this puzzle."],
        kind="text",
    )

    assert message_list(step.instructions) == ["The story begins.", "Solve this puzzle."]
    assert message_list("") == []


def test_media_messages_are_preserved_for_line_replies() -> None:
    image = {
        "type": "image",
        "originalContentUrl": "https://example.com/photo.jpg",
        "previewImageUrl": "https://example.com/preview.jpg",
    }
    step = GameStep(title="Reveal", instructions=[image], kind="text")

    assert message_list(step.instructions) == [image]


def test_map_message_uses_the_map_asset(monkeypatch) -> None:
    monkeypatch.setenv("ASSET_BASE_URL", "https://example.com/assets")

    assert map_message() == {
        "type": "image",
        "originalContentUrl": "https://example.com/assets/map.jpg",
        "previewImageUrl": "https://example.com/assets/map.jpg",
    }


def test_audio_asset_message_uses_asset_url_and_duration(monkeypatch) -> None:
    monkeypatch.setenv("AUDIO_BASE_URL", "https://example.com/assets/voice")

    assert audio_asset_message("clue.m4a", 12_345) == {
        "type": "audio",
        "originalContentUrl": "https://example.com/assets/voice/clue.m4a",
        "duration": 12_345,
    }


def test_audio_asset_message_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        audio_asset_message("clue.m4a", 0)


def test_story_audio_is_configured_in_order() -> None:
    audio_names = []
    for step in GAME_STEPS:
        for story_index in range(len(step.story_chunks)):
            messages = main.step_messages(step, story_index)
            if len(messages) == 2:
                audio_names.append(messages[1]["originalContentUrl"].rsplit("/", 1)[-1])
    for story_index in range(len(GAME_STEPS[-1].ending_chunks)):
        audio_names.append(
            main.story_reply(
                GAME_STEPS[-1].ending_chunks[story_index],
                story_index + 1 < len(GAME_STEPS[-1].ending_chunks),
                sum(len(step.story_chunks) for step in GAME_STEPS)
                - len(main.SILENT_STORY_CHUNKS)
                + story_index,
            )[1]["originalContentUrl"].rsplit("/", 1)[-1]
        )

    assert audio_names == [f"msg{index}.mp3" for index in range(1, 28)]


def test_history_museum_route_story_uses_msg10_audio() -> None:
    messages = main.step_messages(GAME_STEPS[4], story_index=1)

    assert messages[1]["originalContentUrl"].endswith("/msg10.mp3")


def test_history_museum_arrival_story_uses_msg11_audio() -> None:
    messages = main.step_messages(GAME_STEPS[4], story_index=2)

    assert messages[1]["originalContentUrl"].endswith("/msg11.mp3")


def test_temple_clue_story_uses_msg23_audio() -> None:
    messages = main.step_messages(GAME_STEPS[9], story_index=1)

    assert messages[1]["originalContentUrl"].endswith("/msg23.mp3")


async def test_line_client_sends_image_video_and_audio_messages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    line_client = LineClient("access-token", http_client)
    await line_client.reply(
        "reply-token",
        [
            {"type": "image", "originalContentUrl": "https://example.com/photo.jpg", "previewImageUrl": "https://example.com/preview.jpg"},
            {"type": "video", "originalContentUrl": "https://example.com/video.mp4", "previewImageUrl": "https://example.com/video-preview.jpg", "trackingId": "video-1"},
            {"type": "audio", "originalContentUrl": "https://example.com/clue.m4a", "duration": 12345},
        ],
    )
    await http_client.aclose()

    payload = json.loads(requests[0].content)
    assert payload["replyToken"] == "reply-token"
    assert payload["messages"] == [
        {"type": "image", "originalContentUrl": "https://example.com/photo.jpg", "previewImageUrl": "https://example.com/preview.jpg"},
        {"type": "video", "originalContentUrl": "https://example.com/video.mp4", "previewImageUrl": "https://example.com/video-preview.jpg", "trackingId": "video-1"},
        {"type": "audio", "originalContentUrl": "https://example.com/clue.m4a", "duration": 12345},
    ]


async def test_line_client_accepts_a_single_image_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    line_client = LineClient("access-token", http_client)
    await line_client.reply(
        "reply-token",
        {
            "type": "image",
            "originalContentUrl": "https://example.com/map.jpg",
            "previewImageUrl": "https://example.com/map.jpg",
        },
    )
    await http_client.aclose()

    assert json.loads(requests[0].content)["messages"] == [
        {
            "type": "image",
            "originalContentUrl": "https://example.com/map.jpg",
            "previewImageUrl": "https://example.com/map.jpg",
        }
    ]


async def test_line_client_starts_loading_animation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    line_client = LineClient("access-token", http_client)
    await line_client.start_loading("user-id")
    await http_client.aclose()

    assert requests[0].url == "https://api.line.me/v2/bot/chat/loading/start"
    assert json.loads(requests[0].content) == {"chatId": "user-id", "loadingSeconds": 60}


async def test_line_client_gets_profile_with_channel_access_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"displayName": "小明"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    line_client = LineClient("access-token", http_client)
    assert await line_client.get_profile("user-id") == "小明"
    await http_client.aclose()

    assert requests[0].url == "https://api.line.me/v2/bot/profile/user-id"
    assert requests[0].headers["Authorization"] == "Bearer access-token"


async def test_line_client_links_a_rich_menu_to_a_user() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    line_client = LineClient("access-token", http_client)
    await line_client.link_rich_menu("user-id", "richmenu-knowledge")
    await http_client.aclose()

    assert requests[0].method == "POST"
    assert str(requests[0].url) == (
        "https://api.line.me/v2/bot/user/user-id/richmenu/richmenu-knowledge"
    )


async def test_failed_success_reply_still_advances_player(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "users.csv"))
    store.add_user("user-id")

    async def fail_reply(reply_token, message):
        raise httpx.HTTPStatusError(
            "bad reply",
            request=httpx.Request("POST", "https://api.line.me"),
            response=httpx.Response(400),
        )

    monkeypatch.setattr(main.line_client, "reply", fail_reply)

    await main.advance_player(store, "user-id", "reply-token", "Success")

    assert store.get_step_index("user-id") == 1


async def test_success_reply_starts_with_correct_answer_message(tmp_path, monkeypatch) -> None:
    store = UserStore(str(tmp_path / "users.csv"))
    store.add_user("user-id")
    replies = []

    async def capture_reply(reply_token, message):
        replies.append(message)

    monkeypatch.setattr(main.line_client, "reply", capture_reply)

    await main.advance_player(store, "user-id", "reply-token", "Story continues")

    assert replies[0][0] == "沒錯！正確答案！"
    assert replies[0][1] == "Story continues"


async def test_gemini_image_check_requires_clear_subject() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "NO"}]}}]},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gemini_client = main.GeminiClient("api-key", "model", http_client)
    assert not await gemini_client.check_image(
        b"image", "image/jpeg", "The photo must clearly show a computer."
    )
    await http_client.aclose()

    prompt = json.loads(requests[0].content)["systemInstruction"]["parts"][0]["text"]
    assert "Return YES only when the requirement is clearly met" in prompt
    assert "Return NO when the required subject is absent" in prompt
