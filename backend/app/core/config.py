"""全局配置。所有配置项均可用环境变量覆盖（见 .env.example）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq"
    db_echo: bool = False

    jwt_secret: str = "dev-secret-change-me-before-deploy"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720

    default_timezone: str = "Asia/Shanghai"

    # 自助注册开关。公司内部开放时置 true；对外暴露时置 false，改为管理员开号。
    registration_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
