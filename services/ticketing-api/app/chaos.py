"""In-memory chaos state. Process-local on purpose: one replica, one state."""

from __future__ import annotations

import threading

from pydantic import BaseModel, Field


class ChaosState(BaseModel):
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    latency_ms: int = Field(default=0, ge=0, le=10_000)
    db_fault: bool = False


class ChaosController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = ChaosState()

    def get(self) -> ChaosState:
        with self._lock:
            return self._state.model_copy()

    def update(self, **changes) -> ChaosState:
        with self._lock:
            self._state = self._state.model_copy(update=changes)
            # Re-validate the merged state so bounds still hold after model_copy.
            self._state = ChaosState.model_validate(self._state.model_dump())
            return self._state.model_copy()

    def reset(self) -> ChaosState:
        with self._lock:
            self._state = ChaosState()
            return self._state.model_copy()


chaos = ChaosController()
