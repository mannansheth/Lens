def score(content: str) -> float:
    """A deterministic sentiment stub, without downloading a model."""
    return 1.0 if 'happy' in content.lower() else 0.0
