import os
from dataclasses import dataclass


def load_dotenv() -> None:
    dotenv_path = ".env"
    if not os.path.exists(dotenv_path):
        return
    for line in open(dotenv_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    line_channel_access_token: str = ""
    line_channel_secret: str = ""
    user_csv_path: str = "users.csv"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        return cls(
            line_channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
            line_channel_secret=os.getenv("LINE_CHANNEL_SECRET", ""),
            user_csv_path=os.getenv("USER_CSV_PATH", "users.csv"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        )
