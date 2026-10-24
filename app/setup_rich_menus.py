"""Create and configure the application's LINE Rich Menus."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx

from .config import Settings


API_BASE_URL = "https://api.line.me/v2/bot"
DATA_API_BASE_URL = "https://api-data.line.me/v2/bot"
ROOT = Path(__file__).resolve().parent.parent
MAX_RICH_MENU_IMAGE_BYTES = 1_000_000


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _create_rich_menu(
    client: httpx.Client, access_token: str, definition: dict[str, object]
) -> str:
    response = client.post(
        f"{API_BASE_URL}/richmenu",
        headers={**_headers(access_token), "Content-Type": "application/json"},
        json=definition,
    )
    response.raise_for_status()
    rich_menu_id = response.json().get("richMenuId")
    if not isinstance(rich_menu_id, str) or not rich_menu_id:
        raise RuntimeError(f"LINE returned no rich menu ID: {response.text}")
    return rich_menu_id


def _upload_image(client: httpx.Client, access_token: str, rich_menu_id: str, path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Rich Menu image not found: {path}")
    response = client.post(
        f"{DATA_API_BASE_URL}/richmenu/{rich_menu_id}/content",
        headers={**_headers(access_token), "Content-Type": "image/jpeg"},
        content=path.read_bytes(),
    )
    response.raise_for_status()


def _validate_image(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Rich Menu image not found: {path}")
    image_size = path.stat().st_size
    if image_size > MAX_RICH_MENU_IMAGE_BYTES:
        raise ValueError(
            f"Rich Menu image {path} is {image_size:,} bytes; "
            f"LINE allows at most {MAX_RICH_MENU_IMAGE_BYTES:,} bytes. "
            "Recompress the JPEG and try again."
        )


def _create_alias(
    client: httpx.Client, access_token: str, alias_id: str, rich_menu_id: str
) -> None:
    headers = {**_headers(access_token), "Content-Type": "application/json"}
    response = client.post(
        f"{API_BASE_URL}/richmenu/alias",
        headers=headers,
        json={"richMenuAliasId": alias_id, "richMenuId": rich_menu_id},
    )
    response_text = response.text.casefold()
    alias_exists = response.status_code in {400, 409} and (
        "exist" in response_text or "already" in response_text
    )
    if alias_exists:
        response = client.put(
            f"{API_BASE_URL}/richmenu/alias/{alias_id}",
            headers=headers,
            json={"richMenuId": rich_menu_id},
        )
        if response.is_error:
            delete_response = client.delete(
                f"{API_BASE_URL}/richmenu/alias/{alias_id}",
                headers=_headers(access_token),
            )
            delete_response.raise_for_status()
            response = client.post(
                f"{API_BASE_URL}/richmenu/alias",
                headers=headers,
                json={"richMenuAliasId": alias_id, "richMenuId": rich_menu_id},
            )
    if response.is_error:
        raise RuntimeError(
            f"LINE Rich Menu alias operation failed ({response.status_code}): "
            f"{response.text}"
        )
    response.raise_for_status()


def _set_default_menu(client: httpx.Client, access_token: str, rich_menu_id: str) -> None:
    response = client.post(
        f"{API_BASE_URL}/user/all/richmenu/{rich_menu_id}",
        headers=_headers(access_token),
    )
    response.raise_for_status()


def _delete_rich_menu(client: httpx.Client, access_token: str, rich_menu_id: str) -> None:
    response = client.delete(
        f"{API_BASE_URL}/richmenu/{rich_menu_id}",
        headers=_headers(access_token),
    )
    response.raise_for_status()


def _message_action(text: str) -> dict[str, str]:
    return {"type": "message", "text": text}


def _postback_action(data: str, *, open_keyboard: bool = False) -> dict[str, str]:
    action = {"type": "postback", "data": data}
    if open_keyboard:
        action["inputOption"] = "openKeyboard"
    return action


def _uri_action(uri: str) -> dict[str, str]:
    return {"type": "uri", "uri": uri}


def _rich_menu_switch_action(alias_id: str, data: str) -> dict[str, str]:
    return {
        "type": "richmenuswitch",
        "richMenuAliasId": alias_id,
        "data": data,
    }


def _area(x: int, y: int, width: int, height: int, action: dict[str, str]) -> dict[str, object]:
    return {"bounds": {"x": x, "y": y, "width": width, "height": height}, "action": action}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-default",
        action="store_true",
        help="Create and upload menus without making the main menu the default.",
    )
    parser.add_argument(
        "--replace-old",
        action="store_true",
        help=(
            "After the new menus are active, delete the menu IDs currently in "
            "MAIN_RICH_MENU_ID and KNOWLEDGE_RICH_MENU_ID."
        ),
    )
    args = parser.parse_args()

    settings = Settings.from_environment()
    if not settings.line_channel_access_token:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN is required")
    old_menu_ids = {
        rich_menu_id
        for rich_menu_id in (settings.main_rich_menu_id, settings.knowledge_rich_menu_id)
        if rich_menu_id
    }
    alias_suffix = uuid4().hex[:8] if args.replace_old else ""
    main_alias_id = f"main-{alias_suffix}" if alias_suffix else "main"
    knowledge_alias_id = f"knowledge-{alias_suffix}" if alias_suffix else "knowledge"

    main_menu = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "Sanxia Main Menu",
        "chatBarText": "開啟選單",
        "areas": [
            _area(
                0, 0, 1250, 220, _rich_menu_switch_action(main_alias_id, "switch_to_main")
            ),
            _area(
                1250,
                0,
                1250,
                220,
                _rich_menu_switch_action(knowledge_alias_id, "switch_to_knowledge"),
            ),
            _area(0, 220, 833, 733, _postback_action("input_answer", open_keyboard=True)),
            _area(833, 220, 834, 733, _postback_action("map")),
            _area(1667, 220, 833, 733, _postback_action("current_challenge")),
            _area(0, 953, 833, 733, _postback_action("game_instructions")),
            _area(833, 953, 834, 733, _postback_action("hint")),
            _area(1667, 953, 833, 733, _postback_action("restart")),
        ],
    }
    topics = [
        "https://indigo-dye.jimmyn.idv.tw/#topic/sanxia",
        "https://indigo-dye.jimmyn.idv.tw/#topic/indigenous",
        "https://indigo-dye.jimmyn.idv.tw/#topic/persimmon",
        "https://indigo-dye.jimmyn.idv.tw/#topic/hakka",
        "https://indigo-dye.jimmyn.idv.tw/#topic/taiping",
        "https://indigo-dye.jimmyn.idv.tw/#topic/jingliao",
    ]
    knowledge_menu = {
        "size": {"width": 2500, "height": 1686},
        "selected": False,
        "name": "Sanxia Knowledge Menu",
        "chatBarText": "科普資料",
        "areas": [
            _area(
                0, 0, 1250, 220, _rich_menu_switch_action(main_alias_id, "switch_to_main")
            ),
            _area(
                1250,
                0,
                1250,
                220,
                _rich_menu_switch_action(knowledge_alias_id, "switch_to_knowledge"),
            ),
            _area(0, 220, 833, 733, _uri_action(topics[0])),
            _area(833, 220, 834, 733, _uri_action(topics[1])),
            _area(1667, 220, 833, 733, _uri_action(topics[2])),
            _area(0, 953, 833, 733, _uri_action(topics[3])),
            _area(833, 953, 834, 733, _uri_action(topics[4])),
            _area(1667, 953, 833, 733, _uri_action(topics[5])),
        ],
    }
    main_image = ROOT / "assets" / "main-richmenu1.jpg"
    knowledge_image = ROOT / "assets" / "richmenu2.jpg"
    _validate_image(main_image)
    _validate_image(knowledge_image)

    with httpx.Client(timeout=30) as client:
        main_id = _create_rich_menu(client, settings.line_channel_access_token, main_menu)
        _upload_image(
            client,
            settings.line_channel_access_token,
            main_id,
            main_image,
        )
        knowledge_id = _create_rich_menu(
            client, settings.line_channel_access_token, knowledge_menu
        )
        _upload_image(
            client,
            settings.line_channel_access_token,
            knowledge_id,
            knowledge_image,
        )
        _create_alias(client, settings.line_channel_access_token, main_alias_id, main_id)
        _create_alias(
            client, settings.line_channel_access_token, knowledge_alias_id, knowledge_id
        )
        if not args.skip_default:
            _set_default_menu(client, settings.line_channel_access_token, main_id)
        if args.replace_old:
            for old_menu_id in sorted(old_menu_ids - {main_id, knowledge_id}):
                _delete_rich_menu(client, settings.line_channel_access_token, old_menu_id)

    print(json.dumps({
        "MAIN_RICH_MENU_ID": main_id,
        "KNOWLEDGE_RICH_MENU_ID": knowledge_id,
        "MAIN_RICH_MENU_ALIAS": main_alias_id,
        "KNOWLEDGE_RICH_MENU_ALIAS": knowledge_alias_id,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
