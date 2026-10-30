"""Controlled login smoke feature; never included in the default release."""

from __future__ import annotations

from realm_scripts.contracts import Binding, SafePlayerOutput


def _on_login(player: SafePlayerOutput) -> None:
    player.SendSysMessage("Python engine controlled smoke: PASS")


def bindings() -> tuple[Binding, ...]:
    return (Binding(event="PLAYER_ON_LOGIN", callback=_on_login),)
