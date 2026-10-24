from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
import os
import re
from pathlib import Path
from typing import Literal, NotRequired, TypedDict


StepType = Literal["text", "photo", "location"]


class ImageMessage(TypedDict):
    type: Literal["image"]
    originalContentUrl: str
    previewImageUrl: str


class VideoMessage(TypedDict):
    type: Literal["video"]
    originalContentUrl: str
    previewImageUrl: str
    trackingId: NotRequired[str]


class QuickReplyAction(TypedDict):
    type: Literal["message", "location"]
    label: str
    text: NotRequired[str]


class QuickReplyItem(TypedDict):
    type: Literal["action"]
    action: QuickReplyAction


class QuickReply(TypedDict):
    items: list[QuickReplyItem]


class TextMessage(TypedDict):
    type: Literal["text"]
    text: str
    quickReply: NotRequired[QuickReply]


MediaMessage = ImageMessage | VideoMessage
Message = str | TextMessage | MediaMessage
MessageContent = Message | list[Message]


@dataclass(frozen=True)
class GameStep:
    """One editable turn in the adventure."""

    title: str
    instructions: MessageContent
    kind: StepType
    answer: str = ""
    chapter: int = 0
    hint: str = ""
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float = 15
    photo_check: str = ""
    photo_references: tuple[str, ...] = ()
    photo_categories: tuple[str, ...] = ()
    quick_reply_options: tuple[str, ...] = ()
    quick_reply_location: bool = False
    success_message: MessageContent = ""


# The manuscript is the source of truth. Keeping the player-facing text in the
# supplied file prevents the game configuration from silently paraphrasing it.
_STORYLINE_PATH = Path(__file__).resolve().parent.parent / "main-storyline.txt"
_STORYLINE = _STORYLINE_PATH.read_text(encoding="utf-8")


def _asset_message(filename: str) -> ImageMessage:
    base_url = os.getenv("ASSET_BASE_URL", "http://localhost:8000/assets").rstrip("/")
    url = f"{base_url}/{filename}"
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}


def map_message() -> ImageMessage:
    return _asset_message("map.jpg")


def _story_between(start: str, end: str) -> str:
    start_index = _STORYLINE.index(start)
    end_index = _STORYLINE.index(end, start_index + len(start))
    return _STORYLINE[start_index:end_index].strip()


def _story_after(start: str, end: str) -> str:
    start_index = _STORYLINE.index(start) + len(start)
    end_index = _STORYLINE.index(end, start_index)
    return _STORYLINE[start_index:end_index].strip()


def _story_to_end(start: str) -> str:
    return _STORYLINE[_STORYLINE.index(start) + len(start):].strip()


def _story_split_on_heading(start: str, heading: str) -> list[str]:
    before_heading, marker, after_heading = _story_to_end(start).partition(heading)
    if not marker:
        return [_story_to_end(start)]
    return [part.strip() for part in (before_heading, after_heading) if part.strip()]


def _story_narrative(start: str, end: str) -> str:
    text = _story_between(start, end)
    first_line, separator, remainder = text.partition("\n")
    if first_line.strip() == start.strip():
        text = remainder if separator else ""
    return text.strip()


def _story_challenge(start: str, end: str) -> str:
    start_index = _STORYLINE.index(start)
    puzzle_match = re.search(
        r"^謎題(?:｜.*)?\s*$",
        _STORYLINE[start_index:],
        re.MULTILINE,
    )
    if puzzle_match is None:
        raise ValueError(f"Challenge heading not found after {start!r}")
    puzzle_index = start_index + puzzle_match.start()
    end_index = _STORYLINE.index(end, puzzle_index + len("謎題"))
    text = _STORYLINE[puzzle_index:end_index]
    if text.startswith("謎題｜"):
        text = text.removeprefix("謎題｜")
    elif text.startswith("謎題\n"):
        text = text.removeprefix("謎題\n")
    else:
        text = text.removeprefix("謎題")
    return "\n".join(
        line for line in text.strip().splitlines()
        if not line.startswith(("目標地點：", "答案：", "答案:", "關鍵字：", "關鍵字:"))
    ).strip()


# A step is complete when its answer is accepted, its location is close enough,
# or its photo passes the image check. Facilitator-only answer lines are kept in
# the step's answer field and are not included in player-facing story segments.
GAME_STEPS: list[GameStep] = [
    GameStep(
        title="尋找另一口古井",
        chapter=0,
        instructions=[
            _story_narrative("前情提要", "謎題｜尋找另一口古井"),
            _story_challenge("謎題｜尋找另一口古井", "第一章"),
        ],
        kind="location",
        answer="民權街84巷,民權街84巷的古井,古井",
        latitude=24.93317,
        longitude=121.36872,
        quick_reply_location=True,
        success_message="",
    ),
    GameStep(
        title="三峽的傳統產業",
        chapter=1,
        instructions=[
            _story_narrative("第一章", "謎題"),
            _story_challenge("第一章", "答案：藍染"),
        ],
        kind="text",
        answer="藍染,蓝染",
        success_message=_story_between("勾玉：「原來……", "第二章"),
    ),
    GameStep(
        title="藍染製作流程",
        chapter=2,
        instructions=[
            _story_narrative("第二章", "謎題"),
            _story_challenge("第二章", "答案：採收"),
        ],
        kind="text",
        answer="青出於藍",
        success_message=_story_after("關鍵字：青出於染", "第三章"),
    ),
    GameStep(
        title="藍地黃虎旗",
        chapter=3,
        instructions=[
            _story_narrative("第三章", "謎題"),
            _story_challenge("第三章", "完成任務後"),
        ],
        kind="photo",
        photo_check="The photo must clearly show a blue flag or textile with a yellow tiger emblem.",
        success_message=_story_between("完成任務後", "第四章"),
    ),
    GameStep(
        title="庄役場",
        chapter=4,
        instructions=[
            _story_narrative("第四章", "謎題"),
            _story_challenge("第四章", "正確答案：B"),
        ],
        kind="text",
        answer="B,B.辦理地方行政事務,辦理地方行政事務",
        quick_reply_options=("A", "B", "C", "D"),
        success_message=_story_after("正確答案：B. 辦理地方行政事務", "謎題｜消失的染坊文字"),
    ),
    GameStep(
        title="消失的染坊文字",
        chapter=4,
        instructions=[
            _story_challenge("謎題｜消失的染坊文字", "答案：外洋"),
        ],
        kind="text",
        answer="外洋、重資",
        success_message=_story_after("答案：外洋、重資", "第五章"),
    ),
    GameStep(
        title="找回失去的藍",
        chapter=5,
        instructions=[
            _story_narrative("第五章", "謎題"),
            _asset_message("Ch5_Photo.jpg"),
            _story_challenge("第五章", "答案：A(大菁)"),
        ],
        kind="text",
        answer="A,A.大菁,大菁",
        quick_reply_options=("A", "B"),
        success_message=_story_after("答案：A(大菁)", "第六章"),
    ),
    GameStep(
        title="虎姑婆",
        chapter=6,
        instructions=[
            _story_narrative("第六章", "謎題"),
            _story_challenge("第六章", "答案:1905"),
        ],
        kind="text",
        answer="1905",
        success_message=_story_after("答案:1905", "第七章"),
    ),
    GameStep(
        title="祖師廟的印記",
        chapter=7,
        instructions=[
            _story_narrative("第七章", "謎題"),
            _story_challenge("第七章", "答案:照片x3"),
            _asset_message("Ch7_Photo.jpg"),
        ],
        kind="photo",
        photo_check="The photo must clearly show one of the three required subjects at Sanxia Zushi Temple.",
        photo_references=("Ch7_Ref1.jpg", "Ch7_Ref2.jpg", "Ch7_Ref3.jpg"),
        photo_categories=("carved pillar", "bell", "temple exterior"),
        success_message=_story_split_on_heading(
            "答案:照片x3 (百鳥朝梅柱、鐘、祖師廟外觀)", "終章"
        ),
    ),
]


def current_step(step_index: int) -> GameStep | None:
    if 0 <= step_index < len(GAME_STEPS):
        return GAME_STEPS[step_index]
    return None


def message_list(content: MessageContent) -> list[Message]:
    if isinstance(content, str):
        return [content] if content else []
    return [message for message in content if message]


def answer_is_correct(step: GameStep, answer: str) -> bool:
    normalized_answer = _normalize_answer(answer)
    accepted = {
        _normalize_answer(item)
        for item in step.answer.split(",")
        if item.strip()
    }
    return normalized_answer in accepted


def _normalize_answer(value: str) -> str:
    """Normalize chat answers without changing the story's Chinese wording."""
    return re.sub(r"[\s，,。.!！:：；;、]+", "", value).casefold()


def distance_meters(
    latitude: float, longitude: float, target_latitude: float, target_longitude: float
) -> float:
    earth_radius_meters = 6_371_000
    lat_delta = radians(target_latitude - latitude)
    lon_delta = radians(target_longitude - longitude)
    haversine = sin(lat_delta / 2) ** 2 + cos(radians(latitude)) * cos(
        radians(target_latitude)
    ) * sin(lon_delta / 2) ** 2
    return earth_radius_meters * 2 * asin(sqrt(haversine))