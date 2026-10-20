# LINE Photo Bot

This service runs the Sanxia blue-dye time-travel adventure in LINE:

- verifies LINE webhook signatures;
- looks up the sender in a local SQLite database;
- registers a new player and sends the first challenge;
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

3. The complete Sanxia storyline is kept word-for-word in [main-storyline.txt](main-storyline.txt) and loaded by [app/game.py](app/game.py). It includes:

   - the ancient well clue in 民權街 84 巷;
   - blue dye, dyeing-process ordering, and the 青出於染 flag;
   - the 庄役場 and old dye-shop text challenges;
   - the 大菁 plant and 1905 history challenges;
   - three separate Sanxia Zushi Temple photo challenges.

   To customize the adventure, add steps in order with:

   - `kind="text"`: put accepted answers in `answer`, separated by commas.
   - `kind="photo"`: describe what Gemini should verify in `photo_check`.
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
      ],
      ```

      LINE allows up to 5 messages per reply. Both media types need `originalContentUrl` and `previewImageUrl`; video messages may also include `trackingId`.

      `success_message` is sent after a correct response, followed by the next step's instructions. Incoming webhook events, outgoing replies, challenge submissions, and downloaded photo checks are retained in the SQLite database.

   Players can send `開始` or `重來` to restart, and `進度` to see the current challenge. Text answers ignore spaces and common Chinese or English punctuation.

   To reset every player's current progress while keeping the audit history, run:

   ```bash
   python -m app.reset
   ```

4. Run the webhook:

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

Set the LINE Developers webhook URL to `https://YOUR_PUBLIC_HOST/webhook`. LINE requires HTTPS and sends the `x-line-signature` header, which this service validates.

Run tests with `python -m pytest` while the `.venv` is active.
