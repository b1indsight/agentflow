"""Expose the grouped repository API over existing in-memory protocol simulations."""

from typing import cast
from unittest.mock import Mock

from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore


class RepositoryViews:
    """Use the real API surface while delegating behavior to a simulation's existing methods.

    Views are created on access so transaction snapshots contain only simulation state, and
    patches/overrides on that state remain visible to every repository view.
    """

    @property
    def flows(self) -> FlowStore:
        return cast(FlowStore, Mock(spec=FlowStore, wraps=self))

    @property
    def deliveries(self) -> DeliveryStore:
        return cast(DeliveryStore, Mock(spec=DeliveryStore, wraps=self))

    @property
    def jobs(self) -> JobStore:
        return cast(JobStore, Mock(spec=JobStore, wraps=self))

    @property
    def workers(self) -> WorkerStore:
        return cast(WorkerStore, Mock(spec=WorkerStore, wraps=self))

    @property
    def webhooks(self) -> WebhookStore:
        return cast(WebhookStore, Mock(spec=WebhookStore, wraps=self))
