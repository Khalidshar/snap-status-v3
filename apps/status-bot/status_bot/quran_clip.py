"""Status-owned manual Quran clip adapter; WhatsApp gates remain separate."""

from studio_core.religious.quran_commands import main, prepare_clip

APP = "status-bot-v2"
__all__ = ["APP", "prepare_clip"]

if __name__ == "__main__":
    main(APP)
