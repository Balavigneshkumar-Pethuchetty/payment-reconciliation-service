from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/payment_reconciliation"

    # Public base URL of this service (used in QR payloads, webhook callbacks, etc.)
    SERVICE_BASE_URL: str = "https://pay.gm-global-techies-town.club"

    UPI_VPA: str = "society@upi"
    UPI_DISPLAY_NAME: str = "Society"

    HYPERSWITCH_BASE_URL: str = "http://localhost:8080"
    HYPERSWITCH_API_KEY: str = ""

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llava"
    OLLAMA_VISION_MODEL: str = "llava"  # multimodal model for image parsing

    SECRET_KEY: str = "change-me"

    # Default local admin account seeded on first startup
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # Comma-separated list of allowed CORS origins.
    # Defaults cover the main domain, www, and local dev.
    CORS_ORIGINS: str = (
        "https://gm-global-techies-town.club,"
        "https://www.gm-global-techies-town.club,"
        "https://pay.gm-global-techies-town.club,"
        "http://localhost:3000,"
        "http://localhost:5173,"
        "http://localhost:8080"
    )

    # Keycloak — JWKS-only validation, no client secret needed
    KEYCLOAK_URL: str = "https://auth.gm-global-techies-town.club"
    KEYCLOAK_REALM: str = "society-events"
    KEYCLOAK_AUDIENCE: str = "payment-service"  # must match clientId in Keycloak
    # Public Keycloak client that has "Direct Access Grants" (password flow) enabled.
    # Used by POST /auth/login so Keycloak users can authenticate directly.
    KEYCLOAK_LOGIN_CLIENT_ID: str = "society-frontend"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"


settings = Settings()
