"""Alert delivery channels."""

from .email import EmailConfig, EmailError, send_alert_email

__all__ = ["EmailConfig", "EmailError", "send_alert_email"]
