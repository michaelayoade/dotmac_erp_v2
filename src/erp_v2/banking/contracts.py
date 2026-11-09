"""ProductObservation v1 carrying the released Mono connector's neutral payload."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CAPABILITY_ID = "banking.transaction.observation.v1"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationSource(_Contract):
    installation_id: UUID
    connector_key: Literal["mono"]


class DestinationScope(_Contract):
    kind: str = Field(min_length=1, max_length=80)
    ref: str = Field(min_length=1, max_length=240)


class TransactionObservation(_Contract):
    capability_id: Literal["banking.transaction.observation.v1"]
    provider_account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    provider_transaction_id: str = Field(min_length=1, max_length=200)
    amount_minor: str = Field(pattern=r"^(0|[1-9][0-9]{0,17})$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    direction: Literal["credit", "debit"]
    occurred_at: AwareDatetime
    arrival_mode: Literal["poll"]
    narration: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=80)
    balance_minor: str | None = Field(default=None, pattern=r"^-?(0|[1-9][0-9]{0,17})$")

    @property
    def business_date(self) -> date:
        return self.occurred_at.astimezone(ZoneInfo("Africa/Lagos")).date()


class BankingObservationEnvelope(_Contract):
    schema_version: Literal["dotmac.io/product-observation/v1"]
    capability_id: Literal["banking.transaction.observation.v1"]
    contract_version: Literal[1]
    source: ObservationSource
    provider_event_id: str = Field(min_length=1, max_length=200)
    event_type: Literal["banking.transaction.observation.v1"]
    scope: DestinationScope
    observation: TransactionObservation

    @model_validator(mode="after")
    def consistent_identity(self) -> BankingObservationEnvelope:
        if self.provider_event_id != self.observation.provider_transaction_id:
            raise ValueError("provider event and transaction identities disagree")
        return self

    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()
