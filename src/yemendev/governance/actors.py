"""Actors and roles."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


ROLE_RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class Actor(BaseModel):
    model_config = {"frozen": True}

    id: str = Field(min_length=1, max_length=128)
    kind: ActorKind
    role: Role

    @property
    def ref(self) -> str:
        return f"{self.kind.value}:{self.id}"

    def has_role(self, minimum: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]


SYSTEM_ACTOR = Actor(id="yemendev", kind=ActorKind.SYSTEM, role=Role.OPERATOR)
