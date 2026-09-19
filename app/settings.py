from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgresql+asyncpg://engine:engine@localhost:5455/engine"

    # Deliberately NOT named OPENROUTER_API_KEY. A bare name like that is commonly
    # exported in a developer's shell profile, and this project would then silently
    # inherit and spend on whatever key happened to be in the environment. The
    # project-specific name means the key must be set for this project, on purpose.
    openrouter_api_key: str = Field("", alias="FV_OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = "https://firstvoid.com"
    openrouter_app_title: str = "FirstVoid Engine"

    credential_encryption_key: str = ""
    admin_api_key: str = "change-me-in-production"

    env: str = "dev"
    log_level: str = "info"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    # Which OpenAI-compatible endpoint the graph talks to. "openrouter" is the default
    # (one key, one invoice, per-tenant model choice). "ollama" points the same client
    # at a local model server: $0 per token, no data leaves the box, and the answer to
    # "is the cheap local model good enough for this client?" becomes an eval run.
    llm_provider: Literal["openrouter", "ollama"] = "openrouter"
    ollama_base_url: str = "http://localhost:11434/v1"

    # Langfuse tracing. Project-prefixed names on purpose, same reasoning as the
    # OpenRouter key above: a bare LANGFUSE_* in a shell profile must not silently
    # ship a client's transcripts to whatever project it happens to belong to.
    langfuse_public_key: str = Field("", alias="FV_LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field("", alias="FV_LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("https://cloud.langfuse.com", alias="FV_LANGFUSE_HOST")
    max_graph_iterations: int = 6

    # The public URL Twilio signs against. Behind a proxy the app cannot derive it,
    # and a forwarded header is attacker-influenced, so it is configuration.
    public_base_url: str = ""

    # Platform-level Resend key: the agency sends for many clients from its own
    # verified domain. Unset means the dev sink (metadata logged, nothing sent).
    resend_api_key: str = ""
    email_from: str = "FirstVoid Assistant <assistant@firstvoid.com>"

    # S3-compatible object storage. Works with AWS S3, Oracle Object Storage,
    # Cloudflare R2, Backblaze B2 and MinIO - only the endpoint differs.
    backup_endpoint_url: str = ""  # blank = real AWS S3
    backup_bucket: str = "firstvoid-engine-backups"
    backup_region: str = "us-east-1"
    backup_access_key: str = ""
    backup_secret_key: str = ""
    backup_retain_days: int = 30

    # A hard switch, independent of ENV. The environment variable alone is not a
    # safe gate for "may this write to a client's real calendar": an eval replaying
    # thousands of conversations in a production environment would book every one of
    # them for real. Set FV_FORCE_MOCK_TOOLS=true for any bulk replay.
    force_mock_tools: bool = False

    @property
    def use_mock_tools(self) -> bool:
        return self.force_mock_tools or self.env in ("dev", "test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
