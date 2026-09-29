from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Connections and secrets from the environment / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str
    db_password: str
    db_name: str = "cdms"
    # Integration tests only: this database is wiped and re-migrated on every test run.
    db_test_name: str = "cdms_test"

    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False
    db_connect_timeout_s: int = 5

    log_level: str = "INFO"

    # Static bearer token of the Vietful emulator (extension E1). The emulator requires it on its Vietful
    # routes; CDMS sends it when polling. No default: set it in `.env`.
    inventory_api_token: SecretStr | None = None
    # Where CDMS finds the inventory service (the emulator locally; the real Vietful would only change this).
    inventory_base_url: str = "http://localhost:8101"
    # Shared secret of the webhook HMAC (`x-vf-hmacsha256`, D5): CDMS verifies, the emulator signs.
    webhook_secret: SecretStr | None = None

    # /ui console: the emulator URL as the browser sees it (default INVENTORY_BASE_URL; differs in Docker).
    ui_emulator_url: str | None = None
    # /ui console: the webhook URL the emulator must call (default the page's origin; in Docker the service).
    ui_webhook_endpoint: str | None = None
    # Origins allowed to call the emulator from a browser (the CDMS /ui console); a JSON list in the env.
    emulator_cors_origins: list[str] = ["http://localhost:8100", "http://127.0.0.1:8100"]

    # Job lease: a job claimed by a worker that dies is re-claimed after this (D7).
    job_lease_seconds: int = 60

    # Failure-scenario hooks (docs/testing.md F1 / F2, scripts/failure_scenarios.py), never set in normal
    # runs: the process kills itself (os._exit) at the worst moment to prove nothing is lost or duplicated.
    fault_crash_after_inbox_commit: bool = False  # API: webhook committed, response not sent yet
    fault_crash_in_job_batch: bool = False  # worker: webhook batch applied, transaction not committed yet

    def _url(self, database: str) -> str:
        # URL.create escapes special characters in the password.
        url = URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=database,
        )
        return url.render_as_string(hide_password=False)

    @property
    def database_url(self) -> str:
        """Same URL for the async app and sync Alembic."""
        return self._url(self.db_name)

    @property
    def test_database_url(self) -> str:
        return self._url(self.db_test_name)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
