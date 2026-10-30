"""Pinned hook policy for safe first-use realm scripts.

Only hooks both forwarded correctly by privatecore/mod-python-engine at
700a4f75 and suitable for low-frequency scripting are allowed.  Item hooks,
mutable XP/money hooks, high-frequency Unit hooks, and advertised-but-unwired
hooks are intentionally excluded.
"""

from .release_contract import CORE_COMMIT, ENGINE_COMMIT, MANIFEST_SCHEMA, PLAYERBOTS_COMMIT

SAFE_GLOBAL_HOOKS = frozenset(
    {
        "ACCOUNT_ON_ACCOUNT_LOGIN",
        "ACCOUNT_ON_FAILED_ACCOUNT_LOGIN",
        "PLAYER_ON_LOGIN",
        "PLAYER_ON_LOGOUT",
        "PLAYER_ON_PVP_KILL",
        "PLAYER_ON_LEVEL_CHANGED",
        "PLAYER_ON_LOOT_ITEM",
    }
)

SAFE_ENTRY_HOOKS = frozenset(
    {
        "CREATURE_ON_GOSSIP_HELLO",
        "CREATURE_ON_GOSSIP_SELECT",
        "CREATURE_ON_GOSSIP_SELECT_CODE",
        "CREATURE_ON_QUEST_ACCEPT",
        "CREATURE_ON_QUEST_SELECT",
        "CREATURE_ON_QUEST_COMPLETE",
        "CREATURE_ON_QUEST_REWARD",
        "CREATURE_ON_ADD_WORLD",
        "CREATURE_ON_REMOVE_WORLD",
    }
)

SAFE_HOOKS = SAFE_GLOBAL_HOOKS | SAFE_ENTRY_HOOKS

__all__ = [
    "CORE_COMMIT",
    "ENGINE_COMMIT",
    "MANIFEST_SCHEMA",
    "PLAYERBOTS_COMMIT",
    "SAFE_ENTRY_HOOKS",
    "SAFE_GLOBAL_HOOKS",
    "SAFE_HOOKS",
]
