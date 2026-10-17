"""Load configuration without retaining credentials in log output."""

import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_name: str = "Lens"
    database_url: str = field(default="", repr=False)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    embedding_cache_dir: str = ".models"
    log_level: str = "INFO"
    hybrid_alpha: float = 0.5

    @classmethod
    def load(cls) -> "Settings":
        # Lens configuration belongs to Lens, never to the repository being inspected.
        application_root = Path(__file__).resolve().parents[3]
        configuration_file = Path(os.getenv("LENS_ENV_FILE", str(application_root / ".env"))).resolve()
        load_dotenv(configuration_file)
        cache = Path(os.getenv("EMBEDDING_CACHE_DIR", str(application_root / ".models")))
        if not cache.is_absolute():
            cache = configuration_file.parent / cache
        result = cls(
            app_name=os.getenv("APP_NAME", "Lens"),
            database_url=os.getenv("DATABASE_URL", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", cls.embedding_model),
            embedding_device=os.getenv("EMBEDDING_DEVICE", "cpu"),
            embedding_cache_dir=str(cache.resolve()),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            hybrid_alpha=float(os.getenv("HYBRID_ALPHA", "0.5")),
        )
        if not 0 <= result.hybrid_alpha <= 1:
            raise ValueError("HYBRID_ALPHA must be between 0 and 1")
        if result.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be a standard Python logging level")
        return result
