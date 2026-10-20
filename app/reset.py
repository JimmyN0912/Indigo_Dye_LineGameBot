from .config import Settings
from .integrations import UserStore


def main() -> None:
    settings = Settings.from_environment()
    store = UserStore(settings.database_path)
    store.reset_all()
    print(f"Reset all player progress in {settings.database_path}")


if __name__ == "__main__":
    main()