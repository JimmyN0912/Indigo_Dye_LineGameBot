# LINE Photo Bot

This service runs the Sanxia blue-dye time-travel adventure in LINE:

- verifies LINE webhook signatures;
- looks up the sender in a local SQLite database;
- registers a new player and sends the three-message welcome sequence;
- checks the story's text answers and verifies landmark photos with Gemini;
- stores each player's current turn and an audit trail of LINE traffic locally.

## Setup

1. Create a Python 3.11+ environment and install the project:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e '.[dev]'
   ```

2. Copy `.env.example` to `.env` and fill in the LINE channel credentials and Gemini API key. `DATABASE_PATH` defaults to `bot.db`, and downloaded images default to `media/`.

3. The current Sanxia storyline is kept word-for-word in [main-storyline.txt](main-storyline.txt) and loaded by [app/game.py](app/game.py). It includes:

   - the ancient well clue in 民權街 84 巷;
   - blue dye, dyeing-process ordering, and the 青出於染 flag;
   - the 庄役場 and old dye-shop text challenges;
   - the 大菁 plant and 1905 history challenges;
   - the ten challenges in the v2 manuscript, including the dye vat, iron plaque, and seed clues;
   - story sections delivered one chunk at a time with the `請繼續` quick reply;
   - two progressive hints per challenge through the `提示` Rich Menu action.

   To customize the adventure, add steps in order with:

   - `kind="text"`: put accepted answers in `answer`, separated by commas.
   - `kind="photo"`: describe what Gemini should verify in `photo_check`. Chapter 7 also compares each upload with the three `assets/Ch7_Ref*.jpg` reference photos and tracks the matched subjects independently of upload order.
   - `kind="location"`: set the destination `latitude`, `longitude`, and allowed `radius_meters`.

   Every step's `instructions` are sent to the player. Use either one string or a list of strings to send multiple separate LINE messages in one turn:

   ```python
   instructions=["The gate opens.", "Solve this clue:", "What has keys but cannot open locks?"],
   ```

      Replies can also include LINE image or video messages. Media URLs must be HTTPS and publicly reachable by LINE:

      ```python
      success_message=[
         "You found it!",
         {"type": "image", "originalContentUrl": "https://example.com/photo.jpg", "previewImageUrl": "https://example.com/preview.jpg"},
         {"type": "video", "originalContentUrl": "https://example.com/video.mp4", "previewImageUrl": "https://example.com/video-preview.jpg", "trackingId": "adventure-video-1"},
         {"type": "audio", "originalContentUrl": "https://example.com/clue.m4a", "duration": 12345},
      ],
      ```

      Audio messages use `duration` in milliseconds. Audio files can be added to
      Audio files placed in `voice/` are served from `/assets/voice` and can be
      referenced with `audio_asset_message("clue.m4a", 12345)`. Set
      `AUDIO_BASE_URL` to the public HTTPS URL ending in `/assets/voice` before
      starting the bot (for example, `https://YOUR_PUBLIC_HOST/assets/voice`).
      The guide photos continue to use `ASSET_BASE_URL` and `/assets`.

      LINE allows up to 5 messages per reply. Image and video messages need
      `originalContentUrl` and `previewImageUrl`; audio messages need
      `originalContentUrl` and `duration`. Video messages may also include
      `trackingId`. The audio URL must be publicly reachable over HTTPS.

      `success_message` is sent after a correct response, followed by the next step's instructions. Incoming webhook events, outgoing replies, challenge submissions, and downloaded photo checks are retained in the SQLite database.

   On first contact, the bot greets the player by their LINE display name,
   sends the story introduction and game instructions, and offers `開始吧!` as
   a quick reply. Selecting it starts the first story chunk. Players can also
   send `開始` or `重來` to restart, `進度` to see the current challenge, and
   `地圖` or `三峽觀光地圖` to receive the tourism map. Text answers ignore
   spaces and common Chinese or English punctuation.

   When a player follows the Official Account again after unblocking it, LINE
   sends a `follow` event. The bot responds with the same welcome sequence
   without resetting the player's existing progress.

   The main Rich Menu uses a two-tab top bar and six feature actions:

   | Label | Action | Value |
   | --- | --- | --- |
   | 主選單 | Rich Menu switch | `main` alias |
   | 科普資料 | Rich Menu switch | `knowledge` alias |
   | 輸入答案 | Postback | `input_answer` + `openKeyboard` |
   | 地圖 | Postback | `map` |
   | 目前任務 | Postback | `current_challenge` |
   | 遊戲說明 | Postback | `game_instructions` |
   | 提示 | Postback | `hint` |
   | 重新開始 | Postback | `restart` |

   Configure the Rich Menu image and actions in the LINE Official Account
   Manager (or through the LINE Messaging API). The URI action opens directly
   in LINE and does not send a webhook event to this bot.

   To switch menus through the bot, configure two Rich Menu IDs:

   ```dotenv
   MAIN_RICH_MENU_ID=richmenu-main-id
   KNOWLEDGE_RICH_MENU_ID=richmenu-knowledge-id
   ```

   The `科普資料` and `返回主選單` Message actions require these IDs to be
   configured; otherwise the bot reports that the corresponding menu is not
   available.

   Both menus use a 220-pixel top tab bar. The main menu's `科普資料` tab
   switches to the `knowledge` alias, and the knowledge menu's `主選單` tab
   switches back to the `main` alias. The main menu image must therefore be
   2500 × 1686 pixels, with the six feature areas below the tab bar.
   The six feature areas use silent postbacks, so their action data is not
   displayed as a user message. The `輸入答案` postback additionally uses
   `inputOption: "openKeyboard"`; the bot ignores that postback so no bot
   message is sent, while the user can immediately type an answer. The map
   postback replies with the image only.
   The knowledge menu can use six URI actions:

   | Label | URI |
   | --- | --- |
   | 三峽藍染 | `https://indigo-dye.jimmyn.idv.tw/#topic/sanxia` |
   | 原住民族傳統染色 | `https://indigo-dye.jimmyn.idv.tw/#topic/indigenous` |
   | 新埔柿染 | `https://indigo-dye.jimmyn.idv.tw/#topic/persimmon` |
   | 六堆客家藍衫 | `https://indigo-dye.jimmyn.idv.tw/#topic/hakka` |
   | 太平藍 | `https://indigo-dye.jimmyn.idv.tw/#topic/taiping` |
   | 菁寮小菁藍染 | `https://indigo-dye.jimmyn.idv.tw/#topic/jingliao` |

   The knowledge menu reserves a 220-pixel-wide top tab strip for switching
   back to the `main` alias. The six topic links occupy the two rows below it.
   The knowledge-menu image must visibly include this top tab strip so users
   can switch back by tapping it.

   To create and upload the two Rich Menus from the checked-in images, set
   `LINE_CHANNEL_ACCESS_TOKEN` and run:

   ```bash
   python -m app.setup_rich_menus
   ```

   The command creates both menus, uploads `assets/main-richmenu1.jpg` and
   `assets/richmenu2.jpg`, creates or updates the `main` and `knowledge`
   aliases, and sets the first menu as the default. Copy the printed menu IDs into
   `MAIN_RICH_MENU_ID` and `KNOWLEDGE_RICH_MENU_ID`. Use
   `--skip-default` if the main menu should not be linked to all users yet.
   Each Rich Menu image must be JPEG or PNG, use the dimensions declared in its
   menu, and be no larger than 1,000,000 bytes. The setup command checks this
   before making any API requests.

   To replace the menus currently configured in `MAIN_RICH_MENU_ID` and
   `KNOWLEDGE_RICH_MENU_ID`, create and activate the new menus first and then
   delete the old ones with:

   ```bash
   python -m app.setup_rich_menus --replace-old
   ```

   Replacement is intentionally opt-in. With `--replace-old`, the command uses
   fresh alias IDs for the new tab pair, updates the default menu, and then
   deletes the old IDs. This avoids conflicts with aliases that LINE may still
   retain from an earlier setup. Copy the printed menu IDs into the environment
   after the command succeeds. Do not use `--skip-default` with
   `--replace-old`; otherwise the old default menu may remain active for users.

   To reset every player's current progress while keeping the audit history, run:

   ```bash
   python -m app.reset
   ```

4. Run the webhook:

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

Set the LINE Developers webhook URL to `https://YOUR_PUBLIC_HOST/webhook`. LINE requires HTTPS and sends the `x-line-signature` header, which this service validates.

## Group chats

The bot supports LINE group chats. In the LINE Developers Console, enable
**Messaging API > Channel settings > Allow bot to join group chats**. After
adding the bot to a group, LINE sends a `join` event and the bot posts a short
welcome message. Each group member's game progress is stored separately by
their LINE user ID, so members can play independently in the same group.

If the bot still leaves immediately after this setting is enabled, check the
LINE webhook delivery history and the Official Account's chat/group settings.
This application does not call LINE's leave endpoint.

Run tests with `python -m pytest` while the `.venv` is active.
