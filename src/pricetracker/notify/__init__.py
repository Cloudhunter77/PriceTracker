"""Alert delivery channels."""

from .email import EmailConfig, EmailError, send_digest

__all__ = ["EmailConfig", "EmailError", "send_digest"]
