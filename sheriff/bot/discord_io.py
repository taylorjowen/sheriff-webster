"""Discord implementations of the seams: output sink, view conversion, envelope building."""
from __future__ import annotations

import io
import logging

import aiohttp
import discord

from ..seams import Button, EmbedSpec, OutMessage, OutputSink, RecapEnvelope
from ..store import IsolationError

log = logging.getLogger(__name__)

STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
          "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
NO_MENTIONS = discord.AllowedMentions.none()


class StaticView(discord.ui.View):
    """Buttons only; every click is routed through `on_interaction` by custom_id, so
    they keep working after restarts (persistent by construction)."""

    def __init__(self, buttons: list[Button]):
        super().__init__(timeout=None)
        for b in buttons:
            self.add_item(discord.ui.Button(label=b.label, custom_id=b.custom_id, style=STYLES[b.style],
                                            emoji=b.emoji))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return False   # handled in on_interaction


def to_embed(e: EmbedSpec) -> discord.Embed:
    emb = discord.Embed(title=e.title or None, description=e.description or None, color=e.color)
    for name, value, inline in e.fields:
        emb.add_field(name=name, value=value[:1024], inline=inline)
    if e.footer:
        emb.set_footer(text=e.footer)
    return emb


def message_kwargs(msg: OutMessage, for_edit: bool = False) -> dict:
    kw: dict = {"content": msg.content or None, "embeds": [to_embed(e) for e in msg.embeds],
                "allowed_mentions": NO_MENTIONS}
    kw["view"] = StaticView(msg.buttons) if msg.buttons else (None if for_edit else discord.utils.MISSING)
    if msg.image is not None:
        f = discord.File(io.BytesIO(msg.image), filename=msg.image_name)
        kw["attachments" if for_edit else "file"] = [f] if for_edit else f
    return kw


class DiscordSink(OutputSink):
    """Posts to one channel. Enforces the live/sandbox split (§9.1)."""

    def __init__(self, client: discord.Client, channel_id: int, env: str, sandbox_ids: set[int]):
        if env != "live" and channel_id not in sandbox_ids:
            raise IsolationError(f"test runs may only post to sandbox channels (got {channel_id})")
        if env == "live" and channel_id in sandbox_ids:
            raise IsolationError("live mode may not post to a sandbox channel")
        self.client = client
        self.channel_id = channel_id
        self.env = env

    async def _channel(self) -> discord.abc.Messageable:
        ch = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
        return ch  # type: ignore[return-value]

    async def post(self, msg: OutMessage) -> str:
        ch = await self._channel()
        kw = message_kwargs(msg)
        if kw.get("view") is discord.utils.MISSING:
            kw.pop("view")
        m = await ch.send(**kw)
        return str(m.id)

    async def edit(self, ref: str, msg: OutMessage) -> None:
        ch = await self._channel()
        kw = message_kwargs(msg, for_edit=True)
        await ch.get_partial_message(int(ref)).edit(**kw)  # type: ignore[attr-defined]

    async def delete(self, ref: str) -> None:
        ch = await self._channel()
        try:
            await ch.get_partial_message(int(ref)).delete()  # type: ignore[attr-defined]
        except discord.HTTPException:
            pass


async def message_images(message: discord.Message, session: aiohttp.ClientSession) -> list[bytes]:
    out: list[bytes] = []
    for att in message.attachments:
        if (att.content_type or "").startswith("image") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                out.append(await att.read())
            except discord.HTTPException as e:
                log.warning("attachment read failed: %s", e)
    for emb in message.embeds:
        for url in (emb.image.url if emb.image else None, emb.thumbnail.url if emb.thumbnail else None):
            if not url:
                continue
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        out.append(await r.read())
            except Exception as e:
                log.warning("embed image fetch failed: %s", e)
    return out


def message_text(message: discord.Message) -> str:
    """Message content plus any embed text (some apps put the recap text in an embed)."""
    parts = [message.content or ""]
    for emb in message.embeds:
        if emb.title:
            parts.append(emb.title)
        if emb.description:
            parts.append(emb.description)
    return "\n".join(p for p in parts if p)


async def envelope_from_message(message: discord.Message, kind: str, session: aiohttp.ClientSession,
                                with_images: bool = True, edit_seq: int = 0) -> RecapEnvelope:
    return RecapEnvelope(
        kind=kind, message_id=str(message.id), channel_id=str(message.channel.id), author_id=str(message.author.id),
        author_name=getattr(message.author, "display_name", None), text=message_text(message),
        timestamp=message.created_at, edited_at=message.edited_at,
        images=await message_images(message, session) if with_images else [], edit_seq=edit_seq)
