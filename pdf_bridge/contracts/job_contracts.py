"""Neutral data and error contracts shared by the Jenkins job client layers."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pdf_bridge.contracts.schemas import OperationResultInput


class BridgeClientError(RuntimeError):
    """A safe, user-facing error from the Jenkins client boundary."""


class CliModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PullResult(CliModel):
    version: Literal[1] = 1
    batch_id: uuid.UUID | None
    request_id: str
    operation_count: int = Field(ge=0)
    batch_directory: str | None
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    idempotent_replay: bool = False


class ReportFile(CliModel):
    version: Literal[2] = 2
    batch_id: uuid.UUID
    pipeline_run_id: str = Field(min_length=1, max_length=255)
    results: list[OperationResultInput] = Field(min_length=1, max_length=500)

    @field_validator("pipeline_run_id")
    @classmethod
    def normalize_pipeline_run_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("pipeline_run_id must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def unique_results(self) -> ReportFile:
        operation_ids = [result.operation_id for result in self.results]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("results must contain exactly one entry per operation")
        return self


class ClientOptions(CliModel):
    """Validated transport options passed from the CLI manager to the HTTP service."""

    base_url: str
    allowed_host: str
    token_file: Path | None
    timeout_seconds: float
    allow_http: bool
    insecure_skip_tls_verify: bool
    ca_bundle: Path | None
