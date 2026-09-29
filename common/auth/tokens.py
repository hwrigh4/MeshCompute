import hashlib
import secrets


def issue_worker_token() -> tuple[str, str]:
    """Return a high-entropy bearer credential and its storage digest."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()
