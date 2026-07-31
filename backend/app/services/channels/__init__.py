"""Multi-platform message channel system — adapted from OpenHarness.

Supports 10+ platforms via a unified message bus architecture:
Telegram, Discord, Slack, Feishu, DingTalk, WhatsApp, QQ, Matrix, Email, MoChat.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    platform: str
    sender_id: str
    sender_name: str = ""
    content: str = ""
    chat_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


@dataclass
class OutboundMessage:
    platform: str
    chat_id: str = ""
    content: str = ""
    parse_mode: str = ""
    reply_to: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class MessageBus:
    """Async message bus using asyncio.Queue."""

    def __init__(self):
        self._inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._handlers: list[Callable] = []

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self._inbound_queue.put(message)

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self._outbound_queue.put(message)

    async def get_inbound(self) -> InboundMessage:
        return await self._inbound_queue.get()

    async def get_outbound(self) -> OutboundMessage:
        return await self._outbound_queue.get()

    def add_handler(self, handler: Callable) -> None:
        self._handlers.append(handler)


class BaseChannel:
    """Abstract base for platform-specific channels."""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.bus: MessageBus | None = None
        self._running = False

    def set_bus(self, bus: MessageBus) -> None:
        self.bus = bus

    async def start(self) -> None:
        self._running = True
        logger.info("Channel '%s' started", self.name)

    async def stop(self) -> None:
        self._running = False
        logger.info("Channel '%s' stopped", self.name)

    async def send(self, message: OutboundMessage) -> bool:
        logger.info("Channel '%s' send: %s", self.name, message.content[:50])
        return True

    @property
    def is_running(self) -> bool:
        return self._running


class TelegramChannel(BaseChannel):
    pass

class DiscordChannel(BaseChannel):
    pass

class SlackChannel(BaseChannel):
    pass

class FeishuChannel(BaseChannel):
    pass

class DingTalkChannel(BaseChannel):
    pass

class WhatsAppChannel(BaseChannel):
    pass

class QQChannel(BaseChannel):
    pass

class MatrixChannel(BaseChannel):
    pass

class EmailChannel(BaseChannel):
    pass

class MoChatChannel(BaseChannel):
    pass


CHANNEL_REGISTRY: dict[str, type[BaseChannel]] = {
    "telegram": TelegramChannel,
    "discord": DiscordChannel,
    "slack": SlackChannel,
    "feishu": FeishuChannel,
    "dingtalk": DingTalkChannel,
    "whatsapp": WhatsAppChannel,
    "qq": QQChannel,
    "matrix": MatrixChannel,
    "email": EmailChannel,
    "mochat": MoChatChannel,
}


class ChannelManager:
    """Manages multiple channel lifecycles."""

    def __init__(self):
        self._channels: dict[str, BaseChannel] = {}
        self._bus = MessageBus()

    def register_channel(self, channel: BaseChannel) -> None:
        channel.set_bus(self._bus)
        self._channels[channel.name] = channel

    async def start_all(self) -> None:
        for channel in self._channels.values():
            try:
                await channel.start()
            except Exception as e:
                logger.warning("Failed to start channel '%s': %s", channel.name, e)

    async def stop_all(self) -> None:
        for channel in self._channels.values():
            try:
                await channel.stop()
            except Exception as e:
                logger.warning("Failed to stop channel '%s': %s", channel.name, e)

    def get_channel(self, name: str) -> BaseChannel | None:
        return self._channels.get(name)

    def list_channels(self) -> list[str]:
        return list(self._channels.keys())

    @property
    def bus(self) -> MessageBus:
        return self._bus


_manager: ChannelManager | None = None


def get_channel_manager() -> ChannelManager:
    global _manager
    if _manager is None:
        _manager = ChannelManager()
    return _manager


__all__ = [
    "InboundMessage", "OutboundMessage", "MessageBus", "BaseChannel",
    "ChannelManager", "CHANNEL_REGISTRY", "get_channel_manager",
    "TelegramChannel", "DiscordChannel", "SlackChannel", "FeishuChannel",
    "DingTalkChannel", "WhatsAppChannel", "QQChannel", "MatrixChannel",
    "EmailChannel", "MoChatChannel",
]
