import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.game import GameStep, answer_is_correct, distance_meters, message_list
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


def test_user_store_uses_local_csv(tmp_path) -> None:
    csv_path = tmp_path / "users.csv"
    store = UserStore(str(csv_path))

    assert csv_path.read_text() == "user_id,state,step_index\n"
    assert not store.has_user("user-1")

    store.add_user("user-1")

    assert store.has_user("user-1")
    assert store.get_step_index("user-1") == 0
    assert csv_path.read_text() == "user_id,state,step_index\nuser-1,new,0\n"


def test_text_answers_are_case_insensitive_and_support_alternatives() -> None:
    step = GameStep(title="Clue", instructions="Answer", kind="text", answer="north, N")

    assert answer_is_correct(step, " NORTH ")
    assert answer_is_correct(step, "n")
    assert not answer_is_correct(step, "south")


def test_location_distance_is_in_meters() -> None:
    assert distance_meters(0, 0, 0, 0.001) == pytest.approx(111.2, rel=0.01)


def test_game_steps_can_send_multiple_messages() -> None:
    step = GameStep(
        title="Clue",
        instructions=["The story begins.", "Solve this puzzle."],
        kind="text",
    )

    assert message_list(step.instructions) == ["The story begins.", "Solve this puzzle."]


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
