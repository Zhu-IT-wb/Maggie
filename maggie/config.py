from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(override=True)


@dataclass(frozen=True)
class Settings:
    workdir: Path
    provider: str
    model: str
    api_key: str
    openai_base_url: str
    anthropic_base_url: str
    embedding_model: str
    embedding_api_key: str
    embedding_base_url: str
    embedding_dimensions: int
    max_tokens: int


def load_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("MODEL_ID", "deepseek-chat").strip()
    api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    embedding_api_key = (
        os.getenv("EMBEDDING_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or api_key
    ).strip()
    openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    return Settings(
        workdir=Path.cwd(),
        provider=provider,
        model=model,
        api_key=api_key,
        openai_base_url=openai_base_url,
        anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
        embedding_model=os.getenv("EMBEDDING_MODEL_ID", "text-embedding-3-small").strip(),
        embedding_api_key=embedding_api_key,
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL", openai_base_url).rstrip("/"),
        embedding_dimensions=max(int(os.getenv("EMBEDDING_DIMENSIONS", "1024")), 0),
        max_tokens=int(os.getenv("MAX_TOKENS", "8000")),
    )
