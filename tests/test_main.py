import base64
import hashlib
import hmac
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.game import GAME_STEPS, GameStep, answer_is_correct, distance_meters, message_list
from app.integrations import LineClient, UserStore


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


def test_storyline_has_all_required_challenges() -> None:
    assert len(GAME_STEPS) == 9
    assert [step.title for step in GAME_STEPS[:3]] == [
        "尋找另一口古井",
        "三峽的傳統產業",
        "藍染製作流程",
    ]
    assert sum(step.kind == "photo" for step in GAME_STEPS) == 2


def test_photo_guides_and_chapter_seven_references_are_configured() -> None:
    assert GAME_STEPS[6].instructions[-1]["originalContentUrl"].endswith("/Ch5_Photo.jpg")
    chapter_seven = GAME_STEPS[8]
    assert chapter_seven.instructions[-1]["originalContentUrl"].endswith("/Ch7_Photo.jpg")
    assert chapter_seven.photo_references == (
        "Ch7_Ref1.jpg", "Ch7_Ref2.jpg", "Ch7_Ref3.jpg"
    )
    assert chapter_seven.photo_categories == (
        "carved pillar", "bell", "temple exterior"
    )


def test_final_photo_challenge_has_traditional_chinese_labels() -> None:
    assert main.PHOTO_CATEGORY_LABELS == {
        "carved pillar": "百鳥朝梅柱",
        "bell": "鐘",
        "temple exterior": "祖師廟外觀",
    }


def test_final_photo_success_message_includes_the_epilogue() -> None:
    success_messages = message_list(GAME_STEPS[8].success_message)

    assert len(success_messages) == 2
    assert "終章" not in "\n".join(success_messages)
    assert success_messages[0].endswith("勾玉閉上了眼。")
    assert "旅程結束了，但故事還沒有" in success_messages[1]
    assert success_messages[1].startswith("旁白：爸爸媽媽站在勾玉面前")
    assert success_messages[1].endswith("讓三峽的藍，繼續被記得。")


def test_first_turn_separates_story_from_exact_challenge() -> None:
    messages = message_list(GAME_STEPS[0].instructions)

    assert len(messages) == 2
    assert not messages[0].startswith("前情提要")
    assert messages[1] == (
        "尋找另一口古井\n"
        "請從信封袋中取出第ㄧ關道具，仔細觀察照片中的線索，並搭配旅遊地圖，找出照片中的古井所在位置。"
    )
    assert "目標地點" not in messages[1]
    assert "答案" not in messages[1]
    assert "關鍵字" not in messages[1]


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


def test_each_step_uses_its_own_challenge_order() -> None:
    challenges = [
        next(message for message in reversed(message_list(step.instructions)) if isinstance(message, str))
        for step in GAME_STEPS
    ]

    assert "第ㄧ關" in challenges[0]
    assert "第二關" in challenges[1]
    assert "第三關" in challenges[2]
    assert "旗子" in challenges[3]
    assert "庄役場" in challenges[4]
    assert "第四關照片" in challenges[5]
    assert "正確的植物" in challenges[6]
    assert "1905" not in challenges[7]
    assert "祖師廟" in challenges[8]
    assert all("第ㄧ關" not in challenge for challenge in challenges[1:])


def test_chapter_four_challenge_starts_at_heading() -> None:
    challenge = message_list(GAME_STEPS[4].instructions)[-1]

    assert challenge.startswith("「在沒有現代公所的年代")
    assert "這道關於庄役場的謎題" not in challenge


def test_storyline_answers_accept_the_published_solutions() -> None:
    expected_answers = {
        0: "民權街 84 巷",
        1: "藍染",
        2: "青出於藍",
        4: "B",
        5: "外洋、重資",
        6: "A",
        7: "1905",
    }

    for index, answer in expected_answers.items():
        assert answer_is_correct(GAME_STEPS[index], answer)


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


async def test_line_client_sends_image_and_video_messages() -> None:
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
        ],
    )
    await http_client.aclose()

    payload = json.loads(requests[0].content)
    assert payload["replyToken"] == "reply-token"
    assert payload["messages"] == [
        {"type": "image", "originalContentUrl": "https://example.com/photo.jpg", "previewImageUrl": "https://example.com/preview.jpg"},
        {"type": "video", "originalContentUrl": "https://example.com/video.mp4", "previewImageUrl": "https://example.com/video-preview.jpg", "trackingId": "video-1"},
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
