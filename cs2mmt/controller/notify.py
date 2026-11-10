"""Notifications to the owner's phone. Discord only: a bot (two-way) or a channel webhook (one-way).

The Discord bot is the main channel: messages, the owner's commands (!status, !pause ...) and
approvals (✅/❌ reactions) all happen in one private server channel. A plain Discord webhook is
one-way and needs no bot (the C sentinel uses one).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

from ..common.http import HttpClient, HttpError
from ..common.log import redact

__all__ = ["Channel", "DiscordBotChannel", "APPROVE_EMOJI", "REJECT_EMOJI", "DiscordWebhookChannel", "Notifier", "build_channels"]

log = logging.getLogger("cs2mmt.notify")


class Channel:
    name = "channel"

    def send(self, text: str, level: str = "info", approval_id: Optional[str] = None) -> bool:
        raise NotImplementedError


DISCORD_API = "https://discord.com/api/v10"
APPROVE_EMOJI = "\u2705"  # ✅
REJECT_EMOJI = "\u274c"   # ❌


class DiscordBotChannel(Channel):
    """A Discord bot in the owner's private server (two-way, REST only, no gateway connection).

    Messages go to one channel. The controller reads that channel every few seconds for the
    owner's commands (``!status`` ...), and an approval request gets ✅ and ❌ reactions: the owner
    taps one. Reading command text needs the bot's "Message Content Intent" (Developer Portal).
    """

    name = "discord"
    LIMIT = 1900  # Discord's limit is 2000 characters per message

    def __init__(self, http: HttpClient, token: str, channel_id: str, api_base: str = DISCORD_API,
                 on_approval_message: Optional[Callable[[str, str], Any]] = None):
        self.http = http
        self.token = token
        self.channel_id = str(channel_id)
        self.api_base = api_base.rstrip("/")
        self.on_approval_message = on_approval_message

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bot " + self.token,
                "User-Agent": "DiscordBot (https://github.com/kanghyeon01/CS2ModManagementTool, 0.1)"}

    def call(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self.http.request(method, self.api_base + path, headers=self._headers(), json_body=body, params=params)
        if r.status == 204:
            return None
        if not r.ok:
            raise HttpError("discord %s %s: HTTP %d %s" % (method, path, r.status, r.text[:200]), r.status)
        return r.json() if r.body else None

    def send(self, text: str, level: str = "info", approval_id: Optional[str] = None) -> bool:
        prefix = {"error": "\u274c ", "warning": "\u26a0\ufe0f ", "success": "\u2705 "}.get(level, "")
        last_id = None
        for chunk in _split(prefix + text, self.LIMIT):
            msg = self.call("POST", "/channels/%s/messages" % self.channel_id,
                            {"content": chunk, "allowed_mentions": {"parse": []}})
            last_id = (msg or {}).get("id")
        if approval_id and last_id:
            for emoji in (APPROVE_EMOJI, REJECT_EMOJI):
                self.call("PUT", "/channels/%s/messages/%s/reactions/%s/@me" % (self.channel_id, last_id, quote(emoji)))
            if self.on_approval_message:
                self.on_approval_message(approval_id, str(last_id))
        return True

    def messages_after(self, after: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        msgs = self.call("GET", "/channels/%s/messages" % self.channel_id, params=params) or []
        return sorted(msgs, key=lambda m: int(m.get("id", 0)))

    def latest_message_id(self) -> Optional[str]:
        msgs = self.call("GET", "/channels/%s/messages" % self.channel_id, params={"limit": 1}) or []
        return str(msgs[0]["id"]) if msgs else None

    def reaction_user_ids(self, message_id: str, emoji: str) -> List[str]:
        users = self.call("GET", "/channels/%s/messages/%s/reactions/%s" % (self.channel_id, message_id, quote(emoji)),
                          params={"limit": 100}) or []
        return [str(u.get("id")) for u in users]


class DiscordWebhookChannel(Channel):
    """A Discord channel webhook: one-way messages, no bot needed (no commands, no approvals)."""

    name = "discord-webhook"

    def __init__(self, http: HttpClient, url: str):
        self.http = http
        self.url = url

    def send(self, text: str, level: str = "info", approval_id: Optional[str] = None) -> bool:
        if approval_id:
            text += "\n(승인·거절은 봇 메시지의 ✅/❌ 반응 또는 !approve, !reject 명령으로 합니다.)"
        for chunk in _split(text, 1900):
            r = self.http.post(self.url, json_body={"content": chunk, "allowed_mentions": {"parse": []}})
            if not r.ok:
                raise HttpError("discord webhook: HTTP %d %s" % (r.status, r.text[:200]), r.status)
        return True


def _split(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    out: List[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return out


class Notifier:
    """Sends to every channel. ``gate``: whether this controller should speak at all right now
    (the secondary of a redundant pair stays quiet while the primary is up)."""

    def __init__(self, channels: List[Channel], gate: Callable[[], bool] = lambda: True, secrets: Sequence[str] = (), label: str = ""):
        self.channels = channels
        self.gate = gate
        self.secrets = list(secrets)
        self.label = label
        self.failures = 0

    def send(self, text: str, level: str = "info", approval_id: Optional[str] = None, force: bool = False) -> bool:
        if not force and not self.gate():
            log.info("notification suppressed (not the active notifier): %s", text.splitlines()[0] if text else "")
            return False
        text = redact(text, self.secrets)
        if self.label:
            text = "[%s] %s" % (self.label, text)
        delivered = False
        for ch in self.channels:
            try:
                ch.send(text, level=level, approval_id=approval_id)
                delivered = True
            except Exception as e:
                self.failures += 1
                log.warning("notification via %s failed: %s", ch.name, redact(str(e), self.secrets))
        if not self.channels:
            log.info("notification (no channels configured): %s", text)
        return delivered

    def discord(self) -> Optional[DiscordBotChannel]:
        for ch in self.channels:
            if isinstance(ch, DiscordBotChannel):
                return ch
        return None


def build_channels(cfg: Dict[str, Any], secrets: Dict[str, Any], http: HttpClient) -> List[Channel]:
    """From ``[notify]`` and ``[secrets]`` of the controller config."""
    channels: List[Channel] = []
    bot_token = secrets.get("discord_bot_token")
    channel_id = cfg.get("discord_channel_id")
    if bot_token and channel_id:
        channels.append(DiscordBotChannel(http, bot_token, str(channel_id)))
    elif secrets.get("discord_webhook_url"):
        channels.append(DiscordWebhookChannel(http, secrets["discord_webhook_url"]))  # one-way only
    return channels
