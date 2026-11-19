from functools import lru_cache
from pathlib import Path

from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    keitaro_url: str = ""
    keitaro_api_key: str = ""
    database_url: str = f"sqlite:///{(ROOT / 'wrapper.db').as_posix()}"

    # Префикс имён кампаний, которые создаёт обёртка
    campaign_prefix: str = "test_khristoforov_"
    # Куда первый поток отправляет трафик из выбранных стран
    google_url: str = "https://google.com"
    # Значения по умолчанию для формы создания (ищутся по имени через API)
    default_domain: str = ""
    default_group: str = "FORTESTS"
    default_source: str = "FORTESTS"

    # Как часто перечитывать список офферов из Keitaro для автодополнения, секунд
    offers_ttl: int = 60
    # Разрешить в редакторе офферы, которых нет в GET /offers (Keitaro их принимает).
    # На тестовом стенде аккаунту виден один оффер, поэтому там включаем.
    allow_unlisted_offers: bool = False

    # Защита от DNS-rebinding: с какими Host-заголовками принимать запросы
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]

    @field_validator("keitaro_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        # API-ключ уходит в заголовке — по http он ушёл бы открытым текстом
        u = urlparse(v)
        if v and not (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))):
            raise ValueError("KEITARO_URL должен начинаться с https://")
        return v.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
