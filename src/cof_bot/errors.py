"""Exception hierarchy. Every failure the bot raises on purpose is a CofBotError."""


class CofBotError(Exception):
    """Base class for all bot errors."""


class ConfigError(CofBotError):
    """Configuration is missing, malformed, or unsafe."""


class CredentialError(CofBotError):
    """API credentials are absent, rejected, or carry unsafe permissions."""


class ExchangeError(CofBotError):
    """A Coinbase API call failed after retries, or returned an unusable payload."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(ExchangeError):
    """HTTP 429 persisted through every retry."""
