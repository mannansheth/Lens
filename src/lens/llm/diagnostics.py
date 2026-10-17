"""Opt-in source-bearing diagnostic artifacts with known credentials redacted."""
import json
import os
from pathlib import Path
import re


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"api[_-]?key|password|authorization|secret", key, re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if len(secret) >= 8 and re.search(r"KEY|TOKEN|PASSWORD|SECRET|DATABASE_URL", key):
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(://[^\s:/]+:)[^@\s]+@", r"\1[REDACTED]@", value)
        def assignment(match):
            token = match.group(2)
            if token.startswith(("process.env.", "os.environ", "os.getenv(", "Environment.GetEnvironmentVariable(")):
                return match.group(0)  # Source expressions name credentials; they are not key values.
            return match.group(1) + "[REDACTED]"
        value = re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*)[\"']?([^\s,;\"']+)", assignment, value)
        value = re.sub(r"AIza[0-9A-Za-z_-]{35}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}", "[REDACTED]", value)
        value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9_.-]{12,}", r"\1[REDACTED]", value)
    return value


def write_trace(path, value):
    path = Path(path)
    # Diagnostics must never silently overwrite existing user/source files.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(redact(value), stream, indent=2, ensure_ascii=False, default=str)
