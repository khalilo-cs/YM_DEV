"""Platform configuration.

Configuration comes from (in order of precedence): explicit arguments, environment
variables prefixed ``YEMENDEV_``, then defaults. Secrets are never stored here: remote
model API keys are referenced by *environment variable name* only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class SandboxSettings(BaseModel):
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_memory_mb: int = Field(default=2048, ge=64)
    max_file_size_mb: int = Field(default=256, ge=1)
    max_output_bytes: int = Field(default=200_000, ge=1024)
    # "required": refuse to run if network isolation cannot be applied.
    # "best_effort": apply when possible and record whether it was applied.
    # "disabled": do not isolate (recorded in evidence).
    network_isolation: str = Field(default="best_effort")
    python_executable: str = Field(default=sys.executable)

    @field_validator("network_isolation")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in {"required", "best_effort", "disabled"}:
            raise ValueError("network_isolation must be required|best_effort|disabled")
        return v


class ModelSettings(BaseModel):
    """Optional OpenAI-compatible endpoint (llama.cpp server, Ollama, vLLM, hosted)."""

    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None  # name of env var holding the key, never the key itself
    timeout_seconds: float = 60.0
    max_tokens: int = 2048


class Settings(BaseModel):
    data_dir: Path
    # Projects may only be registered under these roots (empty = any directory).
    allowed_project_roots: list[Path] = Field(default_factory=list)
    approval_ttl_seconds: int = Field(default=3600, ge=30, le=7 * 86400)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "yemendev.sqlite3"

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"

    @property
    def checkpoints_dir(self) -> Path:
        return self.data_dir / "checkpoints"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> Settings:
        env = os.environ
        dd = data_dir or Path(env.get("YEMENDEV_DATA_DIR", Path.cwd() / ".yemendev"))
        roots = [Path(p) for p in env.get("YEMENDEV_ALLOWED_ROOTS", "").split(os.pathsep) if p]
        sandbox = SandboxSettings(
            network_isolation=env.get("YEMENDEV_NETWORK_ISOLATION", "best_effort"),
            timeout_seconds=float(env.get("YEMENDEV_SANDBOX_TIMEOUT", "120")),
            python_executable=env.get("YEMENDEV_PYTHON", sys.executable),
        )
        model = ModelSettings(
            base_url=env.get("YEMENDEV_MODEL_BASE_URL") or None,
            model=env.get("YEMENDEV_MODEL_NAME") or None,
            api_key_env=env.get("YEMENDEV_MODEL_API_KEY_ENV") or None,
        )
        return cls(data_dir=dd.resolve(), allowed_project_roots=roots, sandbox=sandbox, model=model)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.keys_dir, self.checkpoints_dir, self.workspaces_dir):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)
