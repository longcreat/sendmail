"""协议适配器。"""

from .imap import IMAPAdapter, InboundEnvelope
from .smtp import SMTPAdapter

__all__ = ["IMAPAdapter", "InboundEnvelope", "SMTPAdapter"]
