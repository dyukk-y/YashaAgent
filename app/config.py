from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    admin_user_ids: str = ""
    database_path: str = "data/yashaagent.sqlite3"
    media_path: str = "/data/media"
    log_level: str = "INFO"
    ai_api_key: str = ""
    ai_model: str = "gpt-4.1-mini"
    webapp_url: str = ""
    webapp_port: int = 8080

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def admins(self) -> set[int]:
        result: set[int] = set()
        for item in self.admin_user_ids.split(","):
            item = item.strip()
            if item:
                try:
                    result.add(int(item))
                except ValueError as exc:
                    raise ValueError(f"ADMIN_USER_IDS must contain integer IDs; got {item!r}") from exc
        return result
