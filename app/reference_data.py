"""Read-only reference data the agent's tools can rely on (mock data for this assignment).

Keeping prices and rates in data, not in prompts, means the LLM never invents them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DATA = Path(__file__).parent / "data"


@dataclass(frozen=True)
class RateCard:
    currency: str
    contingency_pct: float
    day_rates: dict[str, float]

    def canonical_role(self, role: str) -> str | None:
        """Case/whitespace-insensitive lookup; returns the rate-card spelling or None."""
        key = " ".join(role.split()).lower()
        return next((r for r in self.day_rates if r.lower() == key), None)

    def money(self, amount: float) -> str:
        return f"{self.currency} {amount:,.0f}"


def _load_rate_card() -> RateCard:
    raw = json.loads((_DATA / "rate_card.json").read_text())
    return RateCard(raw["currency"], float(raw["contingency_pct"]),
                    {k: float(v) for k, v in raw["day_rates"].items()})


RATE_CARD = _load_rate_card()
