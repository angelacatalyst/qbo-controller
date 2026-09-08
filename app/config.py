"""Application configuration — reads from .env file."""
import os
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # QBO App Credentials
    QBO_CLIENT_ID: str = os.getenv("QBO_CLIENT_ID", "")
    QBO_CLIENT_SECRET: str = os.getenv("QBO_CLIENT_SECRET", "")
    QBO_REDIRECT_URI: str = os.getenv("QBO_REDIRECT_URI", "http://localhost:8000/qbo/callback")
    QBO_ENVIRONMENT: str = os.getenv("QBO_ENVIRONMENT", "sandbox")  # sandbox | production

    # QBO Endpoints
    QBO_AUTH_URL: str = "https://appcenter.intuit.com/connect/oauth2"
    QBO_TOKEN_URL: str = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    QBO_REVOKE_URL: str = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
    QBO_DISCOVERY_URL: str = "https://developer.api.intuit.com/.well-known/openid_configuration"

    @property
    def QBO_BASE_URL(self) -> str:
        if self.QBO_ENVIRONMENT == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"

    QBO_SCOPES: str = "com.intuit.quickbooks.accounting openid profile email phone address"

    # Security
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./qbo_controller.db")

    # App
    APP_NAME: str = os.getenv("APP_NAME", "QBO AI Controller")
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Token expiry buffer (seconds before actual expiry to refresh)
    TOKEN_REFRESH_BUFFER: int = 300


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
