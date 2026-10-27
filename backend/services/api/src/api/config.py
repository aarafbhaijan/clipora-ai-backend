"""API configuration from environment variables, validated at startup."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "staging", "production"] = "local"
    database_url: str
    s3_bucket: str
    aws_region: str = "us-east-1"

    # cognito: verify the Cognito access token. dev: accept "Bearer dev:<email>" (local only).
    auth_mode: Literal["cognito", "dev"] = "cognito"
    cognito_user_pool_id: str | None = None
    cognito_app_client_id: str | None = None

    db_pool_size: int = 5

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.auth_mode == "dev" and self.env != "local":
            raise ValueError("AUTH_MODE=dev is only allowed when ENV=local")
        if self.auth_mode == "cognito" and not (self.cognito_user_pool_id and self.cognito_app_client_id):
            raise ValueError("COGNITO_USER_POOL_ID and COGNITO_APP_CLIENT_ID are required")
        return self

    @property
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
