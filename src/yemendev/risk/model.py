"""Risk engine contracts (directive 5.6: every prediction states event, probability, observations,
horizon, limitations and recommended response)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Prediction(BaseModel):
    kind: str
    subject: str
    event: str
    probability: float | None = Field(default=None, ge=0, le=1)
    probability_kind: str = "empirical_frequency_uncalibrated"
    horizon: str
    supporting_observations: list[str]
    limitations: list[str]
    recommended_response: str


class RiskScore(BaseModel):
    kind: str
    subject: str
    score: float = Field(ge=0, le=1)
    components: dict[str, float]
    explanation: str
    calibrated: bool = False


class Anomaly(BaseModel):
    metric: str
    subject: str
    observed: float
    baseline_median: float
    baseline_mad: float
    robust_z: float | None
    classification: str = "operational"  # never labelled an attack without security evidence
    explanation: str


class InsufficientEvidence(BaseModel):
    analysis: str
    needed: str
    available: str


class DependencyRisk(BaseModel):
    subject: str
    severity: str
    reason: str
    certainty: str


class RiskReport(BaseModel):
    project_id: str
    predictions: list[Prediction]
    risk_scores: list[RiskScore]
    dependency_risks: list[DependencyRisk]
    anomalies: list[Anomaly]
    insufficient_evidence: list[InsufficientEvidence]
    limitations: list[str]
