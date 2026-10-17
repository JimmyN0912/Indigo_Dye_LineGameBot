# LINE Photo Bot

This service runs a turn-by-turn, real-life adventure game in LINE:

- verifies LINE webhook signatures;
- looks up the sender in a local CSV file;
- registers a new player and sends the first challenge;
- checks text answers, verifies photos with Gemini, and verifies LINE location messages;
- stores each player's current turn in a local CSV file.

## Setup

1. Create a Python 3.11+ environment and install the project:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e '.[dev]'
   ```

2. Copy `.env.example` to `.env` and fill in the LINE channel credentials and Gemini API key. `USER_CSV_PATH` defaults to `users.csv`.

3. Edit `GAME_STEPS` in [app/game.py](app/game.py) to write your adventure. Add steps in order with:

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

      `success_message` is sent after a correct response, followed by the next step's instructions. Existing two-column `users.csv` files are upgraded automatically.

4. Run the webhook:

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

Set the LINE Developers webhook URL to `https://YOUR_PUBLIC_HOST/webhook`. LINE requires HTTPS and sends the `x-line-signature` header, which this service validates.

Run tests with `python -m pytest` while the `.venv` is active.
