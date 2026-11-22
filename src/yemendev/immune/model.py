"""Immune findings. ``category`` separates operational anomalies from security findings (directive 5.9)."""

from __future__ import annotations

from pydantic import BaseModel


class ImmuneFinding(BaseModel):
    rule: str
    category: str  # operational | security
    severity: str  # info | low | medium | high
    certainty: str  # observed | inferred
    subject: str
    detail: str
    recommended_action: str


class ImmuneReport(BaseModel):
    project_id: str
    baseline_scan_id: str | None
    findings: list[ImmuneFinding]
    summary: dict[str, int]
    limitations: list[str]
