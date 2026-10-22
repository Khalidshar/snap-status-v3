"""Exact, bounded rejection classifications; never retain provider descriptions."""

REJECTION_DESCRIPTIONS = {
    "Forbidden: bot was blocked by the user": (403, "BOT_BLOCKED_BY_USER"),
    "Forbidden: user is deactivated": (403, "RECIPIENT_DEACTIVATED"),
    "Forbidden: bot can't initiate conversation with a user": (403, "CONVERSATION_NOT_STARTED"),
    "Forbidden: bot can't send messages to bots": (403, "BOT_RECIPIENT_NOT_ALLOWED"),
    "Forbidden: bot is not a member of the channel chat": (403, "BOT_NOT_CHAT_MEMBER"),
    "Forbidden: bot was kicked from the group chat": (403, "BOT_REMOVED_FROM_CHAT"),
    "Forbidden: bot was kicked from the supergroup chat": (403, "BOT_REMOVED_FROM_CHAT"),
    "Bad Request: chat not found": (400, "CHAT_NOT_FOUND"),
    "Bad Request: chat_id is empty": (400, "CHAT_ID_MISSING"),
    "Bad Request: invalid chat_id": (400, "CHAT_ID_INVALID"),
    "Bad Request: CHAT_ID_INVALID": (400, "CHAT_ID_INVALID"),
    "Unauthorized": (401, "TOKEN_NOT_AUTHORIZED"),
}
UNKNOWN_REJECTION = "TELEGRAM_API_REJECTED"
REJECTION_CLASSES = frozenset(
    [UNKNOWN_REJECTION] + [value[1] for value in REJECTION_DESCRIPTIONS.values()]
)


def safe_classification(value: object) -> str:
    if isinstance(value, str) and value in REJECTION_CLASSES:
        return value
    return UNKNOWN_REJECTION


def classify_rejection(status: object, error_code: object, description: object) -> str:
    """Only exact descriptions paired with matching numeric codes are recognized."""
    if (
        type(status) is not int
        or type(error_code) is not int
        or status != error_code
        or not isinstance(description, str)
        or len(description) > 160
    ):
        return UNKNOWN_REJECTION
    match = REJECTION_DESCRIPTIONS.get(description)
    return match[1] if match and match[0] == status else UNKNOWN_REJECTION
