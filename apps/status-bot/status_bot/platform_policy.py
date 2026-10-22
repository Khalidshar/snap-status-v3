"""Status belongs to WhatsApp, never to the Snap Telegram transport.

This guard is intentionally unconditional: environment flags, HUMAN_FRAME and
valid Telegram credentials cannot authorize the wrong application's channel.
Historical receipt reconciliation remains a separate, network-free operation.
"""


class PlatformSeparationError(ValueError):
    """A fixed, secret-free rejection of a retired cross-platform path."""


def reject_status_telegram():
    raise PlatformSeparationError("STATUS_TELEGRAM_DISABLED_BY_PLATFORM_POLICY")
