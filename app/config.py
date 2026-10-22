"""Runtime configuration, read once from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROVIDERS = {
    # Both expose an OpenAI-compatible /chat/completions endpoint.
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}


@dataclass(frozen=True)
class Settings:
    provider: str = os.getenv("LLM_PROVIDER", "groq").lower()
    model: str = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    # Optional different model for the self-check, so the drafter doesn't grade itself.
    review_model: str = os.getenv("REVIEW_MODEL", "")
    api_key: str = os.getenv("GROQ_API_KEY", "ollama")  # Ollama ignores the key
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.3"))
    # For reasoning models (gpt-oss): low/medium/high. Empty = provider default.
    reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "low")
    # Independent sections are drafted concurrently; keep small for free-tier rate limits.
    max_parallel_tasks: int = int(os.getenv("MAX_PARALLEL_TASKS", "3"))
    json_retries: int = int(os.getenv("LLM_JSON_RETRIES", "2"))
    max_revisions: int = int(os.getenv("MAX_REVISIONS", "1"))
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "outputs"))

    @property
    def base_url(self) -> str:
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown LLM_PROVIDER '{self.provider}'. Use one of {list(PROVIDERS)}")
        return os.getenv("LLM_BASE_URL", PROVIDERS[self.provider])


settings = Settings()
