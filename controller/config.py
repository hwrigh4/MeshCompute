from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MESHCOMPUTE_", env_file=".env", extra="ignore"
    )

    database_url: str = (
        "postgresql+psycopg://meshcompute:meshcompute@localhost:5432/meshcompute"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
