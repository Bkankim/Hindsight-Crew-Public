"""ACL: token -> {identity, personal bank, team banks}.

The gateway derives every authorization decision from this map and NEVER from
client-supplied bank hints. Loaded from a JSON file (HC_ACL_FILE), seeded by
scripts/seed-demo.sh when absent.

ACL file shape:
{
  "tokens": {
    "<opaque-token>": {"identity": "alice", "personal": "personal-alice",
                        "teams": ["team-eng"]}
  }
}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Principal:
    identity: str
    personal: str
    teams: tuple = field(default_factory=tuple)

    def allowed_banks(self) -> frozenset:
        return frozenset((self.personal, *self.teams))

    def is_team_bank(self, bank: str) -> bool:
        return bank in self.teams


class ACL:
    def __init__(self, tokens: Dict[str, Principal]):
        self._tokens = tokens

    @classmethod
    def from_dict(cls, data: dict) -> "ACL":
        tokens: Dict[str, Principal] = {}
        for tok, spec in (data.get("tokens") or {}).items():
            ident = spec.get("identity")
            personal = spec.get("personal")
            if not ident or not personal:
                raise ValueError(f"ACL token entry missing identity/personal: {ident!r}")
            teams = tuple(spec.get("teams") or ())
            tokens[tok] = Principal(identity=ident, personal=personal, teams=teams)
        return cls(tokens)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ACL":
        path = path or os.environ.get("HC_ACL_FILE", "secrets/acl.json")
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def principal_for(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        return self._tokens.get(token)

    def all_banks(self) -> frozenset:
        banks: set = set()
        for p in self._tokens.values():
            banks |= p.allowed_banks()
        return frozenset(banks)
