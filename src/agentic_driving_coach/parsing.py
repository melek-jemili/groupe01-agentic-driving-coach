"""Strict validation of the ``TOKEN|Message`` coach output contract.

A response is *valid* only if its first non-empty line is

    TOKEN|Message

where TOKEN (case-insensitive, surrounding whitespace allowed) is exactly one
of NONE, WARNING, ACTUATE. Anything else is *malformed*; the caller must then
use the deterministic fallback policy - malformed output is never treated as
a valid decision (it is recorded, counted, and replaced).

Message sanitization mirrors the original StopSign.lf post-processing:
shell-ish characters are stripped, and messages that echo the prompt
variables are blanked and replaced by the standard message for the token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .messages import CoachToken

_LINE_RE = re.compile(r"^\s*(?P<token>[A-Za-z]+)\s*\|(?P<message>.*)$")

#: Substrings that indicate the model echoed the prompt instead of coaching.
_BANNED_SUBSTRINGS = ("token", "message", "rd=", "v=", "distance_to", "speed=")

#: Standard messages substituted when a valid token arrives with no usable text.
DEFAULT_MESSAGES: dict[CoachToken, str] = {
    CoachToken.NONE: "",
    CoachToken.WARNING: "Slow down and prepare to stop.",
    CoachToken.ACTUATE: "Brake now.",
}


@dataclass(frozen=True)
class ParsedResponse:
    token: CoachToken | None
    message: str
    malformed: bool
    reason: str | None = None


def _sanitize_message(message: str) -> str:
    message = message.replace("$", "").replace("`", "").strip().strip('"').strip("'")
    lowered = message.lower()
    if any(b in lowered for b in _BANNED_SUBSTRINGS):
        return ""
    return message


def parse_coach_output(raw: str) -> ParsedResponse:
    """Parse a raw model response against the strict contract."""
    if not raw or not raw.strip():
        return ParsedResponse(None, "", malformed=True, reason="empty response")

    first_line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    match = _LINE_RE.match(first_line)
    if match is None:
        return ParsedResponse(None, "", malformed=True, reason="no TOKEN|Message line")

    token_text = match.group("token").upper()
    try:
        token = CoachToken(token_text)
    except ValueError:
        return ParsedResponse(None, "", malformed=True, reason=f"unknown token {token_text!r}")

    if token is CoachToken.NONE:
        return ParsedResponse(token, "", malformed=False)

    message = _sanitize_message(match.group("message"))
    if not message:
        message = DEFAULT_MESSAGES[token]
    return ParsedResponse(token, message, malformed=False)
