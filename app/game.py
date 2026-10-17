from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
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


MediaMessage = ImageMessage | VideoMessage
Message = str | MediaMessage
MessageContent = Message | list[Message]


@dataclass(frozen=True)
class GameStep:
    """One editable turn in the adventure."""

    title: str
    instructions: MessageContent
    kind: StepType
    answer: str = ""
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float = 100
    photo_check: str = ""
    success_message: MessageContent = ""


# Edit this list to create the adventure. Steps run from top to bottom.
# For text steps, use comma-separated accepted answers in `answer`.
GAME_STEPS: list[GameStep] = [
    GameStep(
        title="The first clue",
        instructions="Solve this: What has keys but cannot open locks?",
        kind="text",
        answer="piano",
        success_message="Correct. Your next challenge is ready.",
    ),
    GameStep(
        title="Find the item",
        instructions="Send a photo of a computer.",
        kind="photo",
        photo_check="The photo must clearly show the item named in the player's instructions.",
        success_message="Photo accepted. Now send your location from the item.",
    ),
    GameStep(
        title="Prove your location",
        instructions="Send your current location when you arrive.",
        kind="location",
        latitude=22.99794,
        longitude=120.21889,
        radius_meters=150,
        success_message="Location confirmed. You completed the demo adventure!",
    ),
]


def current_step(step_index: int) -> GameStep | None:
    if 0 <= step_index < len(GAME_STEPS):
        return GAME_STEPS[step_index]
    return None


def message_list(content: MessageContent) -> list[Message]:
    if isinstance(content, str):
        return [content]
    return [message for message in content if message]


def answer_is_correct(step: GameStep, answer: str) -> bool:
    accepted = {item.strip().casefold() for item in step.answer.split(",") if item.strip()}
    return answer.strip().casefold() in accepted


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