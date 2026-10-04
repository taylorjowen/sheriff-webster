"""Headless OutputSink: records every would-be post, embed and button as structured objects."""
from __future__ import annotations

import copy
import itertools

from ..seams import OutMessage, OutputSink


class HeadlessSink(OutputSink):
    def __init__(self):
        self._ids = itertools.count(1)
        self.messages: dict[str, OutMessage] = {}
        self.log: list[tuple[str, str, OutMessage]] = []   # (action, ref, message)

    async def post(self, msg: OutMessage) -> str:
        ref = f"h{next(self._ids)}"
        self.messages[ref] = copy.deepcopy(msg)
        self.log.append(("post", ref, copy.deepcopy(msg)))
        return ref

    async def edit(self, ref: str, msg: OutMessage) -> None:
        if ref not in self.messages:
            raise KeyError(ref)
        self.messages[ref] = copy.deepcopy(msg)
        self.log.append(("edit", ref, copy.deepcopy(msg)))

    async def delete(self, ref: str) -> None:
        self.messages.pop(ref, None)
        self.log.append(("delete", ref, OutMessage()))

    def posts(self) -> list[OutMessage]:
        return [m for a, _, m in self.log if a == "post"]
