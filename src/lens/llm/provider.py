"""Small model-independent generation boundary; no execution capabilities."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Generation:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    telemetry: dict | None = None


class LLMProvider(Protocol):
    def health(self) -> dict: ...
    def model_info(self) -> dict: ...
    def generate(self, messages: list[dict], schema: dict | None = None) -> Generation: ...
