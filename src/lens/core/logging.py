"""Small JSON logger; only explicit non-sensitive event fields are emitted."""

import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "event": record.getMessage(),
                **getattr(record, "fields", {}),
            }
        )


def configure(level: str) -> None:
    # SQLGlot's unsupported-syntax warnings can echo SQL text. Our parser reports
    # location-only warnings instead, without recording source contents in logs.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("lens")
    logger.handlers = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    logging.getLogger("lens").log(level, name, extra={"fields": fields})
