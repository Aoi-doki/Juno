"""Home Assistant as the device layer.

One integration rather than per-vendor APIs: anything HA supports, Juno
supports, including hardware bought after this was written. That is the whole
argument for putting HA in the middle instead of talking to vendor clouds.

HA should run on your LAN, not the always-on box — most device protocols
(Matter, Zigbee, mDNS discovery) are local-network-only. The brain reaches it
over Tailscale.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 15.0

# Domains where a mistake is expensive or unsafe. Juno can read their state but
# never call a service on them unless you explicitly allow it, because a
# misheard sentence should not be able to unlock the front door.
GUARDED_DOMAINS = frozenset({"lock", "cover", "alarm_control_panel", "vacuum"})


@dataclass(frozen=True, slots=True)
class Entity:
    entity_id: str
    name: str
    state: str

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def describe(self) -> str:
        return f"{self.entity_id} ({self.name}): {self.state}"


class HomeAssistant:
    def __init__(self, url: str, token: str, forbidden: list[str] | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.forbidden = list(forbidden or [])

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def is_forbidden(self, entity_id: str) -> bool:
        """Config denylist, matched literally or as a ``domain.*`` glob."""
        return any(fnmatch.fnmatch(entity_id, pattern) for pattern in self.forbidden)

    def is_guarded(self, entity_id: str) -> bool:
        return entity_id.split(".", 1)[0] in GUARDED_DOMAINS

    async def states(self) -> list[Entity]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.get(f"{self.url}/api/states", headers=self._headers())
            response.raise_for_status()
            raw = response.json()

        entities = []
        for item in raw:
            entity_id = item.get("entity_id", "")
            if not entity_id or self.is_forbidden(entity_id):
                continue
            entities.append(
                Entity(
                    entity_id=entity_id,
                    name=str((item.get("attributes") or {}).get("friendly_name", entity_id)),
                    state=str(item.get("state", "unknown")),
                )
            )
        return entities

    async def call(self, domain: str, service: str, entity_id: str, **data) -> str:
        if self.is_forbidden(entity_id):
            return f"{entity_id} is on the forbidden list"
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(
                f"{self.url}/api/services/{domain}/{service}",
                headers=self._headers(),
                json={"entity_id": entity_id, **data},
            )
            response.raise_for_status()
        return f"called {domain}.{service} on {entity_id}"
