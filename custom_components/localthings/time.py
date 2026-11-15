"""Time platform for Local Things."""

from __future__ import annotations

import datetime
from typing import Any, cast

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import LocalThingsCoordinator
from .entity import LocalThingsEntity, async_add_bound_entities
from .registry.entities import TimeDesc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LocalThingsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_bound_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda b: isinstance(b.desc, TimeDesc),
        lambda b: LocalThingsTime(coordinator, b),
    )


class LocalThingsTime(LocalThingsEntity, TimeEntity):
    # Moves with the clock every minute while a washer or dryer sits idle; keeping it
    # out of the recorder spares a new attributes row each time.
    _unrecorded_attributes = frozenset({"earliest_finish"})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        desc = cast(TimeDesc, self._bound.desc)
        if desc.extra_state_attributes_fn is None:
            return None
        rep = self.coordinator.entity_rep(self._bound.href or "")
        return desc.extra_state_attributes_fn(rep, self._resources)

    @property
    def native_value(self) -> datetime.time | None:
        value = (self.coordinator.data or {}).get(self._state_key)
        desc = cast(TimeDesc, self._bound.desc)
        if desc.native_value_fn is not None:
            return desc.native_value_fn(value, dt_util.now())
        # UTC-backed descriptors (such as delay-finish projections)
        # hand over an aware instant for localization. Wall-clock fields
        # already supply a time and need no timezone conversion.
        if isinstance(value, datetime.datetime):
            return dt_util.as_local(value).time()
        return value

    async def async_set_value(self, value: datetime.time) -> None:
        desc = cast(TimeDesc, self._bound.desc)
        payload = desc.payload_fn(value, dt_util.now()) if desc.payload_fn else value
        await self.coordinator.async_send_command(self._bound, payload)
