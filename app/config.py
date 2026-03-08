from __future__ import annotations

from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "srilanka_pois"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Gemini
    gemini_api_key: str = ""

    # Auth
    api_keys: List[str] = []
    require_auth: bool = True

    # App
    app_version: str = "1.0.0"

    @field_validator("api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v: str | list) -> list:
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @field_validator("api_keys")
    @classmethod
    def keys_must_be_strong(cls, keys: list) -> list:
        for key in keys:
            if len(key) < 32:
                raise ValueError(
                    f"API key '{key[:6]}...' is too short ({len(key)} chars). "
                    "Minimum 32 characters required. "
                    "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
                )
        return keys

    @model_validator(mode="after")
    def auth_requires_keys(self) -> "Settings":
        if self.require_auth and not self.api_keys:
            raise ValueError(
                "REQUIRE_AUTH=true but API_KEYS is empty. "
                "Either set API_KEYS or set REQUIRE_AUTH=false for local stdio use."
            )
        return self

    model_config = {"env_file": ".env", "case_sensitive": False}


# Singleton — imported everywhere
settings = Settings()
