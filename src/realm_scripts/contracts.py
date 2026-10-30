"""Small, intentionally conservative contract exposed to realm features.

The upstream engine exports more mutable AzerothCore methods than this file.  A
method being callable does not make it an approved authority boundary.  Feature
code should depend on these protocols and request a reviewed contract change
when it genuinely needs another operation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, overload

type Callback = Callable[..., object]


class PlayerView(Protocol):
    """Read-mostly player surface proven by the pinned engine bindings."""

    def GetGUID(self) -> int: ...

    def GetAccountId(self) -> int: ...

    def GetName(self) -> str: ...

    def GetLevel(self) -> int: ...

    def GetClass(self) -> int: ...

    def GetRace(self, original: bool = False) -> int: ...

    def GetMoney(self) -> int: ...

    def GetZoneId(self) -> int: ...

    def GetAreaId(self) -> int: ...

    def IsInCombat(self) -> bool: ...


class SafePlayerOutput(PlayerView, Protocol):
    """Narrow reviewed mutation surface for operator-facing messages only."""

    def SendSysMessage(self, message: str) -> None: ...

    def SendNotification(self, message: str) -> None: ...


class AzerothCoreAPI(Protocol):
    """Registration overloads exported by ``azerothcore.Register``."""

    @overload
    def Register(self, event_name: str, callback: Callback) -> None: ...

    @overload
    def Register(self, event_name: str, callback: Callback, entry_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Binding:
    """A declarative hook registration returned by one feature module."""

    event: str
    callback: Callback
    entry_id: int | None = None


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Deterministic summary printed into the worldserver Python log."""

    release: str
    features: tuple[str, ...]
    binding_count: int
