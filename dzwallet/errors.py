class DzWalletError(Exception):
    """Base class of all SDK errors."""


class ConfigurationError(DzWalletError):
    """A key is missing."""


class ConnectionFailed(DzWalletError):
    """Timeout or network error. After initiate_payment the outcome is UNKNOWN: call payment_status() first."""


class InvalidSignatureError(DzWalletError):
    """A notification is not authentic, or is for another order or amount."""


class ApiError(DzWalletError):
    def __init__(self, message, errors=None, http_status=None):
        super().__init__(message)
        self.errors = errors or []
        self.http_status = http_status

    @property
    def is_rate_limited(self):
        return self.http_status == 429
