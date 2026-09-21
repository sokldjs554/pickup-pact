from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from .models import ReconcileResult


@dataclass(frozen=True)
class Review:
    summary: str
    hypotheses: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    provider: str


class ReviewProvider(Protocol):
    def review(self, result: ReconcileResult) -> Review: ...


class OfflineReviewProvider:
    """Deterministic CI-safe reviewer. It never invents evidence outside the result packet."""

    def review(self, result: ReconcileResult) -> Review:
        hypotheses = tuple(f"Investigate: {item}" for item in result.anomalies) or ("No anomaly detected",)
        return Review(
            summary=f"{len(result.anomalies)} anomaly(s), {len(result.repairs)} deterministic repair action(s).",
            hypotheses=hypotheses,
            evidence_event_ids=tuple(result.evidence_event_ids),
            provider="offline",
        )


class ClaudeReviewProvider:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("AI_MODEL", "claude-sonnet-4-5")

    def review(self, result: ReconcileResult) -> Review:
        from anthropic import Anthropic  # optional dependency

        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=self.model,
            max_tokens=700,
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(result.model_dump(mode="json"))}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        return _parse_review(text, result, "claude")


class OpenAIReviewProvider:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("AI_MODEL", "gpt-5")

    def review(self, result: ReconcileResult) -> Review:
        from openai import OpenAI  # optional dependency

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response = client.responses.create(
            model=self.model,
            instructions=_SYSTEM,
            input=json.dumps(result.model_dump(mode="json")),
        )
        return _parse_review(response.output_text, result, "openai")


class GeminiReviewProvider:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("AI_MODEL", "gemini-2.5-pro")

    def review(self, result: ReconcileResult) -> Review:
        from google import genai  # optional dependency

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model=self.model,
            contents=f"{_SYSTEM}\n\n{json.dumps(result.model_dump(mode='json'))}",
        )
        return _parse_review(response.text or "{}", result, "gemini")


_SYSTEM = """Return JSON only with keys summary, hypotheses, evidence_event_ids.
Use only evidence_event_ids present in the input. Do not propose direct financial mutation.
State uncertainty. Deterministic repair actions in the input are authoritative."""


def _parse_review(text: str, result: ReconcileResult, provider: str) -> Review:
    payload = json.loads(text)
    allowed = set(result.evidence_event_ids)
    evidence = tuple(event_id for event_id in payload.get("evidence_event_ids", []) if event_id in allowed)
    hypotheses = tuple(str(x) for x in payload.get("hypotheses", []))
    return Review(str(payload.get("summary", "")), hypotheses, evidence, provider)


def provider_from_env() -> ReviewProvider:
    name = os.getenv("AI_PROVIDER", "offline").lower()
    if name == "claude":
        return ClaudeReviewProvider()
    if name in {"openai", "chatgpt"}:
        return OpenAIReviewProvider()
    if name == "gemini":
        return GeminiReviewProvider()
    return OfflineReviewProvider()
