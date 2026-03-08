from __future__ import annotations

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
    # Declared as str — pydantic-settings v2 tries to JSON-decode List[str] from .env,
    # which breaks comma-separated values. Parse via the api_keys_list property instead.
    api_keys: str = ""
    require_auth: bool = True

    # App
    app_version: str = "1.0.0"

    @property
    def api_keys_list(self) -> list[str]:
        """Parsed, validated API keys as a list. Use this everywhere — not api_keys."""
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @field_validator("api_keys")
    @classmethod
    def keys_must_be_strong(cls, raw: str) -> str:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        for key in keys:
            if len(key) < 32:
                raise ValueError(
                    f"API key '{key[:6]}...' is too short ({len(key)} chars). "
                    "Minimum 32 characters required. "
                    "Generate: python -c \"import secrets; print(secrets.token_hex(32))\""
                )
        return raw

    @model_validator(mode="after")
    def auth_requires_keys(self) -> "Settings":
        if self.require_auth and not self.api_keys_list:
            raise ValueError(
                "REQUIRE_AUTH=true but API_KEYS is empty. "
                "Either set API_KEYS or set REQUIRE_AUTH=false for local stdio use."
            )
        return self

    model_config = {"env_file": ".env", "case_sensitive": False, "extra": "ignore"}


# Singleton — imported everywhere
settings = Settings()
