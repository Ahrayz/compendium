from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/compendium"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-5"

    google_cloud_project: str = ""
    vertex_location: str = "us-central1"

    max_cost_usd_per_day: float = 5.0

    # Embeddings. The dimension is baked into `chunks.embedding vector(1536)`, so
    # changing either of these is a migration plus a full re-embed, not a config edit.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Full prompts and answers are logged for every query. Invaluable while
    # building; turn off wherever logs are retained and questions may be private.
    log_prompts: bool = True

    # Fetched sources are cached here so re-chunking never re-hits the network.
    corpus_cache_dir: str = ".cache/corpus"

    # Liveness is served on every one of these. Platforms reserve paths without
    # documenting it — GCP's shared frontend answers `/healthz` itself and the
    # request never reaches the container — so don't stake the endpoint on one
    # string. Comma-separated; override with HEALTH_PATHS.
    health_paths: str = "/livez,/healthz,/health"

    @property
    def health_path_list(self) -> list[str]:
        return [p.strip() for p in self.health_paths.split(",") if p.strip()]


@lru_cache
def settings() -> Settings:
    return Settings()
