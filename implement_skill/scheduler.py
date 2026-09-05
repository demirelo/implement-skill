"""Global, fail-closed resource accounting for one implementation campaign.

The campaign has two nested kinds of parallel work (items and Builder candidates), plus gate,
forge, and model boundaries.  Keeping their limits and accounting in one object prevents an
individual adapter from accidentally bypassing the run's budget.  The scheduler is intentionally
small and deterministic: reservations are made before work starts, usage is charged exactly once,
and malformed provider accounting is an error rather than an optimistic zero.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


_ACTIVE = threading.local()


class ResourceLimitError(RuntimeError):
    """Raised when a campaign cannot safely remain within its declared budget."""


@dataclass(frozen=True)
class ResourceBudget:
    """Hard limits for one campaign.

    The defaults are bounded but intentionally generous for a small local campaign.  A caller can
    pass a mapping (for example, profile ``prefs.limits``) to override them.  ``None`` is not a
    supported escape hatch: an explicit finite number is required for every resource.
    """

    max_item_concurrency: int = 8
    max_builder_concurrency: int = 8
    max_verification_cpu: int = 2
    max_api_calls: int = 256
    max_elapsed_seconds: float = 3600.0
    max_tokens: int = 2_000_000
    max_cost_usd: float = 100.0
    token_price_usd: float = 0.00001

    @classmethod
    def from_value(cls, value: ResourceBudget | Mapping[str, Any] | None) -> ResourceBudget:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scheduler budget must be ResourceBudget or a mapping")
        aliases = {
            "items": "max_item_concurrency",
            "builders": "max_builder_concurrency",
            "verification_cpu": "max_verification_cpu",
            "api_calls": "max_api_calls",
            "elapsed_seconds": "max_elapsed_seconds",
            "tokens": "max_tokens",
            "cost_usd": "max_cost_usd",
        }
        canonical_fields = set(cls.__dataclass_fields__)
        allowed_fields = canonical_fields | set(aliases)
        unknown = [key for key in value if key not in allowed_fields]
        if unknown:
            labels = ", ".join(repr(key) for key in sorted(unknown, key=str))
            raise ValueError(f"unknown scheduler budget field(s): {labels}")

        fields = {}
        for alias, canonical in aliases.items():
            has_canonical = canonical in value
            has_alias = alias in value
            if has_canonical and has_alias and value[canonical] != value[alias]:
                raise ValueError(
                    f"conflicting scheduler budget values for {canonical!r} and {alias!r}"
                )
            if has_canonical:
                fields[canonical] = value[canonical]
            elif has_alias:
                fields[canonical] = value[alias]
        fields.update({key: value[key] for key in canonical_fields if key in value})
        # Preserve explicit ``None`` so __post_init__ rejects it. Silently replacing a caller's
        # unbounded value with a default would turn a malformed budget into an unsafe one.
        return cls(**fields)

    def __post_init__(self) -> None:
        integer_fields = (
            "max_item_concurrency", "max_builder_concurrency", "max_verification_cpu",
            "max_api_calls", "max_tokens",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_elapsed_seconds", "max_cost_usd", "token_price_usd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True)
class ResourceUsage:
    """Detached usage snapshot suitable for a campaign result or audit record."""

    items_started: int = 0
    builder_calls: int = 0
    verification_calls: int = 0
    api_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


@dataclass
class _MutableUsage:
    items_started: int = 0
    builder_calls: int = 0
    verification_calls: int = 0
    api_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


class Scheduler:
    """One thread-safe resource budget shared by every campaign worker and boundary."""

    def __init__(self, budget: ResourceBudget | Mapping[str, Any] | None = None, *, clock=time.monotonic):
        self.budget = ResourceBudget.from_value(budget)
        self._clock = clock
        self._started = clock()
        self._usage = _MutableUsage()
        self._lock = threading.RLock()
        self._items = threading.BoundedSemaphore(self.budget.max_item_concurrency)
        self._builders = threading.BoundedSemaphore(self.budget.max_builder_concurrency)
        self._verification = threading.BoundedSemaphore(self.budget.max_verification_cpu)
        self._local = threading.local()

    @contextmanager
    def activate(self):
        """Make this scheduler visible to nested gate/reviewer helpers on this thread."""
        previous = getattr(_ACTIVE, "scheduler", None)
        _ACTIVE.scheduler = self
        try:
            yield self
        finally:
            _ACTIVE.scheduler = previous

    @classmethod
    def current(cls) -> Scheduler | None:
        """Return the scheduler active on the calling thread, if any."""
        # Each Scheduler owns a local, so keep a process-level binding for helpers that do not
        # receive the object explicitly.  It is replaced/restored by ``activate`` below.
        return getattr(_ACTIVE, "scheduler", None)

    def _elapsed(self) -> float:
        return max(0.0, float(self._clock() - self._started))

    def _check_elapsed(self) -> None:
        if self._elapsed() > self.budget.max_elapsed_seconds:
            raise ResourceLimitError("campaign elapsed-time budget exceeded")

    def _reserve(self, resource: str) -> None:
        with self._lock:
            self._check_elapsed()
            if resource == "item":
                self._usage.items_started += 1
            elif resource == "builder":
                self._usage.builder_calls += 1
            elif resource == "verification":
                self._usage.verification_calls += 1
            elif resource == "api":
                if self._usage.api_calls >= self.budget.max_api_calls:
                    raise ResourceLimitError("campaign API-call budget exceeded")
                self._usage.api_calls += 1
            else:  # pragma: no cover - internal misuse should be loud
                raise ValueError(f"unknown scheduler resource: {resource}")

    @contextmanager
    def _slot(self, resource: str, semaphore: threading.BoundedSemaphore):
        self._check_elapsed()
        # Polling keeps elapsed limits enforceable even while all slots are occupied.  No
        # unbounded blocking call is allowed to outlive the campaign deadline.
        while True:
            self._check_elapsed()
            remaining = self.budget.max_elapsed_seconds - self._elapsed()
            if remaining < 0:
                raise ResourceLimitError("campaign elapsed-time budget exceeded")
            if semaphore.acquire(timeout=min(0.25, max(0.001, remaining))):
                break
        try:
            self._reserve(resource)
            yield
        finally:
            semaphore.release()

    @contextmanager
    def item_slot(self):
        with self._slot("item", self._items):
            yield

    @contextmanager
    def builder_slot(self):
        with self._slot("builder", self._builders):
            yield

    @contextmanager
    def verification_slot(self):
        with self._slot("verification", self._verification):
            yield

    def account_api(self, *, tokens: int, cost_usd: float) -> None:
        """Charge one completed API call with validated deterministic accounting.

        Provider adapters must provide concrete numbers.  Unknown, negative, NaN, or fractional
        token values are rejected; this avoids silently allowing an unmetered model response to
        cross a campaign cap.
        """
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ResourceLimitError("API response has invalid token accounting")
        if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
            raise ResourceLimitError("API response has invalid cost accounting")
        if not math.isfinite(float(cost_usd)) or cost_usd < 0:
            raise ResourceLimitError("API response has invalid cost accounting")
        with self._lock:
            self._check_elapsed()
            if self._usage.tokens + tokens > self.budget.max_tokens:
                raise ResourceLimitError("campaign token budget exceeded")
            if self._usage.cost_usd + float(cost_usd) > self.budget.max_cost_usd + 1e-12:
                raise ResourceLimitError("campaign cost budget exceeded")
            self._usage.tokens += tokens
            self._usage.cost_usd += float(cost_usd)

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        """Deterministically estimate tokens for a text-only callback boundary.

        The estimate is deliberately conservative and stable across runs; provider envelopes with
        an exact ``usage`` field should call :meth:`account_api` with those values instead.
        """
        if value is None:
            return 0
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            value = str(value)
        return (len(value.encode("utf-8")) + 3) // 4

    def wrap_callback(self, callback: Callable[..., Any], *, role: str = "model") -> Callable[..., Any]:
        """Meter a model/reviewer callback without changing its return contract."""
        # ``run_campaign`` passes its scheduler-wrapped dispatch runner into ``run_implement``
        # and nested review repairs.  Make wrapping idempotent for this scheduler so one logical
        # model boundary is never charged once per stack frame.
        if getattr(callback, "_implement_scheduler", None) is self:
            return callback

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if role.lower().startswith("builder"):
                with self.builder_slot(), self._api_slot():
                    result = callback(*args, **kwargs)
            else:
                with self._api_slot():
                    result = callback(*args, **kwargs)
            tokens, cost = self._result_accounting(result, args, role=role)
            self.account_api(tokens=tokens, cost_usd=cost)
            return result
        setattr(wrapped, "_implement_scheduler", self)
        return wrapped

    @contextmanager
    def _api_slot(self):
        # API concurrency is deliberately bounded by the call budget itself.  The slot reserves
        # exactly one call before crossing the external boundary, including failures.
        self._reserve("api")
        yield

    def wrap_runner(self, runner: Callable[..., Any]) -> Callable[..., Any]:
        """Meter forge/API subprocesses while leaving git and local process calls uncharged."""
        if getattr(runner, "_implement_scheduler", None) is self:
            return runner

        def wrapped(argv: Any, *args: Any, **kwargs: Any) -> Any:
            words = list(argv) if isinstance(argv, (list, tuple)) else []
            api = bool(words and (str(words[0]) in {"gh", "curl", "wget"}))
            if api:
                with self._api_slot():
                    result = runner(argv, *args, **kwargs)
                    self.account_api(tokens=0, cost_usd=0.0)
                    return result
            self._check_elapsed()
            return runner(argv, *args, **kwargs)
        setattr(wrapped, "_implement_scheduler", self)
        return wrapped

    def _result_accounting(self, result: Any, args: tuple[Any, ...], *, role: str) -> tuple[int, float]:
        usage = result.get("usage") if isinstance(result, Mapping) else None
        if isinstance(result, Mapping) and usage is None:
            usage = result.get("meta", {}).get("usage") if isinstance(result.get("meta"), Mapping) else None
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ResourceLimitError(f"{role} returned malformed usage accounting")
            tokens = usage.get("total_tokens")
            if tokens is None:
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if prompt is None or completion is None:
                    raise ResourceLimitError(f"{role} returned incomplete token accounting")
                tokens = prompt + completion
            cost = usage.get("cost_usd", usage.get("cost"))
            if cost is None:
                cost = float(tokens) * self.budget.token_price_usd
            return tokens, cost
        text = result if isinstance(result, str) else str(result or "")
        prompt = args[0] if args else ""
        tokens = self.estimate_tokens(prompt) + self.estimate_tokens(text)
        return tokens, tokens * self.budget.token_price_usd

    def snapshot(self) -> ResourceUsage:
        with self._lock:
            return ResourceUsage(
                items_started=self._usage.items_started,
                builder_calls=self._usage.builder_calls,
                verification_calls=self._usage.verification_calls,
                api_calls=self._usage.api_calls,
                tokens=self._usage.tokens,
                cost_usd=self._usage.cost_usd,
                elapsed_seconds=self._elapsed(),
            )
