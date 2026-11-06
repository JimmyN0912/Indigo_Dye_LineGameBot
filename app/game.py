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


class AudioMessage(TypedDict):
    type: Literal["audio"]
    originalContentUrl: str
    duration: int


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


MediaMessage = ImageMessage | VideoMessage | AudioMessage
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
    hints: tuple[str, str] = ("", "")
    story_chunks: tuple[str, ...] = ()
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float = 15
    photo_check: str = ""
    photo_references: tuple[str, ...] = ()
    photo_categories: tuple[str, ...] = ()
    quick_reply_options: tuple[str, ...] = ()
    quick_reply_location: bool = False
    success_message: MessageContent = ""
    ending_chunks: tuple[str, ...] = ()


# The manuscript is the source of truth. Keeping the player-facing text in the
# supplied file prevents the game configuration from silently paraphrasing it.
_STORYLINE_PATH = Path(__file__).resolve().parent.parent / "main-storyline.txt"
_STORYLINE = _STORYLINE_PATH.read_text(encoding="utf-8")


def _asset_message(filename: str) -> ImageMessage:
    base_url = os.getenv("ASSET_BASE_URL", "http://localhost:8000/assets").rstrip("/")
    url = f"{base_url}/{filename}"
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}


def audio_asset_message(filename: str, duration_ms: int) -> AudioMessage:
    """Build a LINE audio message for an audio file hosted in the voice directory."""
    if duration_ms <= 0:
        raise ValueError("Audio duration must be greater than zero milliseconds")
    base_url = os.getenv("AUDIO_BASE_URL", "http://localhost:8000/assets/voice").rstrip("/")
    return {
        "type": "audio",
        "originalContentUrl": f"{base_url}/{filename}",
        "duration": duration_ms,
    }


def map_message() -> ImageMessage:
    return _asset_message("map.jpg")


def _audio_message(filename: str, duration_ms: int) -> AudioMessage:
    return audio_asset_message(filename, duration_ms)


_STORY_AUDIO_DURATIONS_MS = (
    81006, 23092, 48457, 15386, 21185, 34534, 28160, 29832, 45741,
    10109, 30955, 35344, 42841, 28186, 59193, 117864, 22805, 15987,
    27350, 79256, 16536, 42789, 11546, 14942, 19513, 64235, 85995,
)


def story_audio_message(index: int) -> AudioMessage:
    if not 0 <= index < len(_STORY_AUDIO_DURATIONS_MS):
        raise IndexError(f"Story audio index out of range: {index}")
    return _audio_message(f"msg{index + 1}.mp3", _STORY_AUDIO_DURATIONS_MS[index])


def _story_between(start: str, end: str) -> str:
    start_index = _STORYLINE.index(start)
    end_index = _STORYLINE.index(end, start_index + len(start))
    return _STORYLINE[start_index:end_index].strip()


def _story_after(start: str, end: str) -> str:
    start_index = _STORYLINE.index(start)
    line_end = _STORYLINE.find("\n", start_index)
    start_index = len(_STORYLINE) if line_end == -1 else line_end + 1
    end_index = _STORYLINE.index(end, start_index)
    return _STORYLINE[start_index:end_index].strip()


def _story_to_end(start: str) -> str:
    return _STORYLINE[_STORYLINE.index(start) + len(start):].strip()


def _story_target(text: str) -> str:
    target_matches = list(re.finditer(r"🌟\s*([^，。！？；：、)）\n]+)", text))
    target_match = target_matches[-1] if target_matches else None
    if not target_match:
        return ""
    return re.sub(r"[「」『』\"“”]", "", target_match.group(1)).strip()


def _normalize_route_target(value: str) -> str:
    return re.sub(r"[\s，,。.!！?？:：；;、]+", "", value).casefold()


def _extract_route_hints() -> dict[str, tuple[str, ...]]:
    route_hints: dict[str, tuple[str, ...]] = {}
    story_lines: list[str] = []
    route_target = ""
    hint_lines: list[str] = []
    in_route_hint = False

    for line in _STORYLINE.splitlines():
        stripped_line = line.strip()
        if re.match(r"^(?:😶‍🌫️)?路線提示", stripped_line):
            if in_route_hint and route_target and hint_lines:
                key = _normalize_route_target(route_target)
                route_hints[key] = (*route_hints.get(key, ()), "\n".join(hint_lines))
                hint_lines = []
            route_target = _story_target("\n".join(story_lines)) or route_target
            in_route_hint = True
            continue
        if in_route_hint:
            if not stripped_line:
                if route_target and hint_lines:
                    key = _normalize_route_target(route_target)
                    route_hints[key] = (*route_hints.get(key, ()), "\n".join(hint_lines))
                hint_lines = []
                in_route_hint = False
            else:
                hint_lines.append(stripped_line)
            continue
        story_lines.append(line)

    if route_target and hint_lines:
        key = _normalize_route_target(route_target)
        route_hints[key] = (*route_hints.get(key, ()), "\n".join(hint_lines))
    return route_hints


_ROUTE_HINTS = _extract_route_hints()


def route_hint_for_target(target: str, hint_number: int = 1) -> str:
    hints = _ROUTE_HINTS.get(_normalize_route_target(target), ())
    if not hints:
        return ""
    return hints[min(max(hint_number, 1) - 1, len(hints) - 1)]


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


def _clean_story(text: str) -> str:
    cleaned_lines = []
    in_hint = False
    in_route_hint = False
    for line in text.splitlines():
        stripped_line = line.strip()
        if re.match(r"^(?:😶‍🌫️)?路線提示", stripped_line):
            in_route_hint = True
            continue
        if in_route_hint:
            if not stripped_line:
                in_route_hint = False
            continue
        if not stripped_line:
            if not in_hint:
                cleaned_lines.append("")
            continue
        if re.fullmatch(r"[（(]請繼續[）)]", stripped_line):
            continue
        if stripped_line in {"前情提要", "第一章", "第二章", "第三章", "第四章", "第五章", "第六章", "第七章", "終章"}:
            continue
        if stripped_line.startswith(("答案：", "答案:", "正確答案：", "正確答案:", "謎題答案：", "謎題答案:", "關鍵字：", "關鍵字:")):
            continue
        if stripped_line.startswith("🔔提示"):
            in_hint = True
            continue
        if in_hint:
            if stripped_line.startswith(("旁白", "：", ":", "勾玉", "婦人", "虎姑婆", "照片文字", "「", "（", "😶")):
                in_hint = False
            else:
                continue
        cleaned_lines.append(re.sub(r"^旁白\s*[:：]?\s*", "", stripped_line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()


def _story_chunk_parts(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    current_lines: list[str] = []
    in_route_hint = False

    for line in text.splitlines():
        stripped_line = line.strip()
        if re.match(r"^(?:😶‍🌫️)?路線提示", stripped_line):
            cleaned = _clean_story("\n".join(current_lines))
            if cleaned:
                chunks.append(cleaned)
            current_lines = []
            in_route_hint = True
            continue
        if in_route_hint:
            if not stripped_line:
                in_route_hint = False
            continue
        if re.fullmatch(r"[（(]請繼續[）)]", stripped_line):
            cleaned = _clean_story("\n".join(current_lines))
            if cleaned:
                chunks.append(cleaned)
            current_lines = []
            continue
        current_lines.append(line)

    cleaned = _clean_story("\n".join(current_lines))
    if cleaned:
        chunks.append(cleaned)
    return tuple(chunks)


def _story_chunks(start: str, end: str) -> tuple[str, ...]:
    return _story_chunk_parts(_story_between(start, end))


def _story_chunks_after(start: str, end: str) -> tuple[str, ...]:
    return _story_chunk_parts(_story_after(start, end))


def _story_chunks_to_end(start: str) -> tuple[str, ...]:
    return _story_chunk_parts(_story_to_end(start))


def _challenge(start: str, heading: str, answer_marker: str, include_answer_marker: bool = False) -> str:
    start_index = _STORYLINE.index(start)
    puzzle_index = _STORYLINE.index(heading, start_index)
    answer_index = _STORYLINE.index(answer_marker, puzzle_index)
    hint_index = _STORYLINE.find("\n🔔提示", puzzle_index, answer_index)
    end_index = hint_index if hint_index != -1 else answer_index
    if include_answer_marker:
        end_index = answer_index + len(answer_marker)
        while end_index < len(_STORYLINE) and _STORYLINE[end_index] in "。！？.!?":
            end_index += 1
    text = _STORYLINE[puzzle_index:end_index]
    text = re.sub(r"^🌟", "", text, count=1)
    text = re.sub(r"\n🔔提示一.*?(?=\n🔔提示二)", "", text, flags=re.DOTALL)
    text = re.sub(r"\n🔔提示二.*", "", text, flags=re.DOTALL)
    text = re.sub(r"\n🔔提示[一二].*", "", text, flags=re.DOTALL)
    return _clean_story(text)


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
    GameStep("尋找另一口古井", _challenge("前情提要", "🌟謎題ㄧ｜尋找另一口古井", "答案：目標地點"), "location", "民權街84巷,民權街84巷的古井,古井", story_chunks=_story_chunks("前情提要", "🌟謎題ㄧ｜尋找另一口古井"), latitude=24.93317, longitude=121.36872, quick_reply_location=True, hints=("請查找藍染地圖之圖示一到三，並根據圖示方向前進。", "請查找藍染地圖之圖示一到三，並根據提示一方向前進，到指定地點後，再根據提示二右轉進入巷子，進入後找到提示三的位置即可解謎。")),
    GameStep("找出藍染的線索", _challenge("第一章", "🌟謎題二｜找出藍染的線索", "謎題答案：碾布石"), "text", "碾布石", chapter=1, story_chunks=_story_chunks("第一章", "🌟謎題二｜找出藍染的線索"), hints=("請查找並仔細觀察藍染地圖之圖示三。", "請查找並仔細觀察藍染地圖圖示三上的文字敘述。")),
    GameStep("藍染製作流程", _challenge("第二章", "🌟謎題三｜拼出藍染工序", "答案：採藍&浸泡"), "text", "青出於染", chapter=2, story_chunks=_story_chunks_after("謎題答案：碾布石", "🌟謎題三｜拼出藍染工序"), hints=("請點閱「科普資料」尋找正確解答。", "請點閱「科普資料」中三峽藍染尋找正確解答。")),
    GameStep("藍地黃虎旗", _challenge("第三章", "🌟謎題四｜旗子的秘密", "完成第四關", include_answer_marker=True), "photo", chapter=3, story_chunks=_story_chunks_after("關鍵字：青出於染", "🌟謎題四｜旗子的秘密"), photo_check="The photo must clearly show a blue flag or textile with a yellow tiger emblem.", hints=("答案就藏在青出於染工作坊，請仔細觀察。", "進入青出於染工作坊後，請抬頭尋找解答。")),
    GameStep("庄役場", _challenge("第四章", "🌟謎題五｜庄役場​​知多少", "正確答案：B"), "text", "B,B.辦理地方行政事務,辦理地方行政事務", chapter=4, story_chunks=_story_chunks_after("完成第四關", "🌟謎題五｜庄役場​​知多少"), quick_reply_options=("A", "B", "C", "D"), hints=("請仔細觀察信封中的謎題五照片（早期庒役場照片）。", "請仔細觀察信封中的謎題五照片（早期庒役場照片）。")),
    GameStep("染缸上的文字", _challenge("正確答案：B", "🌟謎題六｜染缸上的文字", "答案：染"), "text", "染", chapter=4, story_chunks=_story_chunks_after("正確答案：B", "🌟謎題六｜染缸上的文字"), hints=("請仔細觀察文物館左邊的染缸", "請從面對染缸的方向，由左邊數到右邊的第三個染缸。")),
    GameStep("消失的染坊文字", _challenge("答案：染", "謎題七｜消失的染坊文字", "答案：外洋、重資"), "text", "外洋、重資", chapter=4, story_chunks=_story_chunks_after("答案：染", "謎題七｜消失的染坊文字"), hints=("根據藍染之路圖示十，染坊會在三峽老街的左側（以照片方位為主）。", "根據藍染之路圖示十，染坊會在三峽老街的左側（以照片方位為主）。")),
    GameStep("代表希望的幼苗", [_asset_message("Challenge8_Photo.jpg"), _challenge("第五章", "🌟謎題八｜代表希望的幼苗", "答案：A(大菁)")], "text", "A,A.大菁,大菁", chapter=5, story_chunks=_story_chunks_after("答案：外洋、重資", "🌟謎題八｜代表希望的幼苗"), quick_reply_options=("A", "B"), hints=("請觀察阿姨唱的兩段歌謠中，歌詞對植物的描述", "對生植物、花瓣顏色為紫色")),
    GameStep("鑄鐵板上的文字", _challenge("第六章", "🌟謎題九｜鑄鐵板上的文字", "答案:葉中藍染而清"), "text", "葉中藍染而清", chapter=6, story_chunks=_story_chunks_after("答案：A(大菁)", "🌟謎題九｜鑄鐵板上的文字"), hints=("請尋找櫻花道上的鑄鐵板《三峽著名傳統產業發展歷程》", "鑄鐵板《三峽著名傳統產業發展歷程》中的「藍染」段落")),
    GameStep("回家的方法", [_asset_message("Challenge10_Photo.jpg"), _challenge("第七章", "謎題十｜回家的方法", "答案:照片x3")], "photo", "", chapter=7, story_chunks=_story_chunks_after("答案:葉中藍染而清", "謎題十｜回家的方法"), photo_check="The photo must clearly show one of the three required subjects at Sanxia Zushi Temple.", photo_references=("Ch7_Ref1.jpg", "Ch7_Ref2.jpg", "Ch7_Ref3.jpg"), photo_categories=("carved pillar", "bell", "temple exterior"), hints=("照片分別為外觀一張、內部一樓一張、二樓一張", "三張線索圖所對應的建築為百鳥朝梅柱、鐘、祖師廟外觀"), ending_chunks=_story_chunks_after("答案:照片x3", "終章") + _story_chunks_to_end("終章")),
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