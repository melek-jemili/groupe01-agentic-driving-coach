"""LLM prompt for the stop-sign coach.

The system prompt is adapted from the preamble of StopSign.lf in
agentic-driving-coach: same rules, same strict single-line output contract.
It is a *text* prompt for a text-only LLM; no images are involved
in the coach input.
"""

from __future__ import annotations

from .messages import StateSnapshot

STOP_SIGN_SYSTEM_PROMPT = (
    "You are an agentic driving coach. "
    "Output exactly ONE line: TOKEN|Message\n"
    "TOKEN must be one of: NONE, WARNING, ACTUATE\n"
    "Message must be one short sentence, about the approaching stop sign only. "
    "distance_to_stop is s, measured in meters. "
    "velocity is v, measured in m/s only.\n"
    "Rules for TOKEN:\n"
    "if s is less than or equal to 25 and v is greater than 2.5 then TOKEN=ACTUATE\n"
    "else if s is between 50 and 60 and v is not between 8 and 10 then TOKEN=WARNING\n"
    "else if s is greater than or equal to 99 and v is not between 8 and 12 then TOKEN=WARNING\n"
    "else TOKEN=NONE\n"
    "If TOKEN=NONE, output: NONE|\n"
    "Do not output anything else."
)


def build_user_prompt(snapshot: StateSnapshot) -> str:
    """The per-request user message, formatted exactly as in the original."""
    return f"distance_to_stop={snapshot.distance_m:.2f}m speed={snapshot.velocity_mps:.2f}m/s"


def build_messages(snapshot: StateSnapshot) -> list[dict[str, str]]:
    """Chat-format messages for the Ollama API."""
    return [
        {"role": "system", "content": STOP_SIGN_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(snapshot)},
    ]
