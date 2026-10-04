"""Identity resolution (§5.5). Identity = Discord user id; display names are never identity."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .seams import IdentityResolver, Unresolved
from .store import Store


def _norm(name: str) -> str:
    return " ".join(name.strip().lstrip("@").split()).casefold()


class LiveIdentityResolver(IdentityResolver):
    """Resolves against the guild's current members plus the bot-maintained name_history."""

    def __init__(self, store: Store, guild_getter):
        self.store = store
        self._guild = guild_getter   # callable -> discord.Guild | None

    def _member_names(self, member: Any) -> list[tuple[str, str]]:
        out = []
        if getattr(member, "nick", None):
            out.append((member.nick, "nick"))
        if getattr(member, "global_name", None):
            out.append((member.global_name, "display"))
        if getattr(member, "display_name", None):
            out.append((member.display_name, "display"))
        out.append((member.name, "username"))
        return out

    def record_member(self, member: Any, at: datetime) -> None:
        for name, kind in self._member_names(member):
            self.store.record_name(str(member.id), name, kind, at)

    def player_for_user(self, user_id: str, display_name: str | None, at: datetime) -> int:
        row = self.store.player_by_discord(user_id)
        if row:
            if display_name and display_name != row["display_name"]:
                self.store.set_display_name(row["player_id"], display_name)
            return row["player_id"]
        return self.store.create_player(display_name or f"user {user_id}", discord_user_id=str(user_id), joined_at=at)

    async def resolve_mention(self, user_id: str, at: datetime) -> int | Unresolved:
        guild = self._guild()
        member = guild.get_member(int(user_id)) if guild else None
        name = member.display_name if member else None
        if member:
            self.record_member(member, at)
        return self.player_for_user(str(user_id), name, at)

    def link_name(self, name: str, pid: int, at: datetime) -> None:
        row = self.store.player(pid)
        if row and row["discord_user_id"]:
            self.store.record_name(row["discord_user_id"], name.strip().lstrip("@"), "link", at)

    async def resolve_name(self, name: str, at: datetime) -> int | Unresolved:
        key = _norm(name)
        # explicit admin links win
        linked = {r["discord_user_id"] for r in self.store.name_matches(name.strip().lstrip("@")) if r["kind"] == "link"}
        if len(linked) == 1:
            return self.player_for_user(linked.pop(), None, at)
        ids: set[str] = set()
        guild = self._guild()
        if guild:
            for m in guild.members:
                if any(_norm(n) == key for n, _ in self._member_names(m)):
                    ids.add(str(m.id))
        for row in self.store.name_matches(name.strip().lstrip("@")):
            ids.add(row["discord_user_id"])
        if len(ids) == 1:
            uid = ids.pop()
            member = guild.get_member(int(uid)) if guild else None
            return self.player_for_user(uid, member.display_name if member else None, at)
        if not ids:
            return Unresolved("no match")
        return Unresolved("ambiguous", sorted(ids))


class SyntheticIdentityResolver(IdentityResolver):
    """Test mode: resolves only within the run's synthetic players. Never touches real members."""

    def __init__(self, store: Store):
        self.store = store
        # placeholder mention id -> player_id, and name history per player
        self.mentions: dict[str, int] = {}
        self.names: dict[int, set[str]] = {}
        self.links: dict[str, int] = {}       # admin links win over everything else

    def add_player(self, name: str, mention_id: str | None, at: datetime) -> int:
        pid = self.store.create_player(name, discord_user_id=None, synthetic=True, joined_at=at)
        if mention_id:
            self.mentions[str(mention_id)] = pid
        self.names[pid] = {_norm(name)}
        return pid

    def rename(self, pid: int, new_name: str) -> None:
        self.store.set_display_name(pid, new_name)
        self.names.setdefault(pid, set()).add(_norm(new_name))

    async def resolve_mention(self, user_id: str, at: datetime) -> int | Unresolved:
        pid = self.mentions.get(str(user_id))
        return pid if pid is not None else Unresolved("unknown placeholder mention")

    async def resolve_name(self, name: str, at: datetime) -> int | Unresolved:
        key = _norm(name)
        if key in self.links:
            return self.links[key]
        current = [p["player_id"] for p in self.store.players() if _norm(p["display_name"]) == key]
        if len(current) == 1:
            return current[0]
        if len(current) > 1:
            return Unresolved("ambiguous", current)
        hist = [pid for pid, names in self.names.items() if key in names]
        if len(hist) == 1:
            return hist[0]
        return Unresolved("ambiguous" if hist else "no match", hist)

    def link_name(self, name: str, pid: int, at: datetime) -> None:
        self.links[_norm(name)] = pid
