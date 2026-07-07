"""WebSocket connection manager — broadcasts live tracking + escalation events."""
import asyncio
import json
import logging
from typing import Any, Dict, List, Set
from uuid import UUID

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        # keyed by "topic:resource_id"
        self.topics: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, topic: str) -> None:
        await ws.accept()
        async with self._lock:
            self.topics.setdefault(topic, set()).add(ws)

    async def disconnect(self, ws: WebSocket, topic: str) -> None:
        async with self._lock:
            if topic in self.topics:
                self.topics[topic].discard(ws)
                if not self.topics[topic]:
                    self.topics.pop(topic, None)

    async def broadcast(self, topic: str, payload: Dict[str, Any]) -> None:
        msg = json.dumps(payload, default=str)
        async with self._lock:
            conns = list(self.topics.get(topic, set()))
        dead: List[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception as e:
                logger.warning("ws send fail: %s", e)
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.topics.get(topic, set()).discard(ws)


manager = ConnectionManager()


def booking_topic(booking_id: UUID) -> str:
    return f"booking:{booking_id}"


def user_topic(user_id: UUID) -> str:
    return f"user:{user_id}"
