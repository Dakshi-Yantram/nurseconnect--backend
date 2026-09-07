"""Minimal stand-ins for third-party packages, so the payout/pricing logic can
be exercised without a database or a package install.

WHY THIS EXISTS
---------------
The repo's own test suite (tests/conftest.py) is integration-style: it drives a
running FastAPI server over HTTP against a live Postgres. That is the right
way to test this feature end to end, and it remains the way to do it. But it
cannot run in an environment with no server, no database and no network.

This harness closes part of that gap. It stubs ONLY third-party imports —
sqlalchemy, pydantic_settings, dotenv-driven config. Every line of
application code under test (payout_service, pricing_engine, billing_service,
invoice_pdf) is the real module, imported normally.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------
Proves: the payout release state machine, duplicate/failure/pending/retry
handling, the Razorpay status mapping, the money arithmetic, and PDF rendering.
These are pure logic and are genuinely exercised.

Does NOT prove: that the SQL is valid, that the ORM mappings match the
migration, that FastAPI wiring is correct, or that a real RazorpayX call
succeeds. Those need the real environment — see the report.

`install()` must be called BEFORE importing any app module.
"""
from __future__ import annotations

import sys
import types
from typing import Any


class _Sentinel:
    """Stands in for anything SQLAlchemy returns at class-definition time.

    Accepts any construction, attribute access, call or subscript and returns
    another sentinel. That is enough for `models.py` to be imported: the
    module only *builds* column descriptors at import, it never evaluates
    them, so a permissive object is indistinguishable from the real thing
    until a query actually runs — and queries here go to a fake session.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    def __call__(self, *args: Any, **kwargs: Any) -> "_Sentinel":
        return _Sentinel(*args, **kwargs)

    def __getattr__(self, name: str) -> "_Sentinel":
        return _Sentinel()

    def __getitem__(self, key: Any) -> "_Sentinel":
        return _Sentinel()

    # Comparison/boolean operators are used to build WHERE clauses at call
    # time (e.g. `WorkerPayout.id == payout_id`), so they must not raise.
    def __eq__(self, other: Any) -> "_Sentinel":  # type: ignore[override]
        return _Sentinel()

    def __ne__(self, other: Any) -> "_Sentinel":  # type: ignore[override]
        return _Sentinel()

    def __lt__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __gt__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __le__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __ge__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __and__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __or__(self, other: Any) -> "_Sentinel":
        return _Sentinel()

    def __hash__(self) -> int:
        return id(self)

    def __bool__(self) -> bool:
        return True


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    # Anything not explicitly set resolves to a sentinel.
    mod.__getattr__ = lambda _n: _Sentinel()  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


def _install_sqlalchemy() -> None:
    if "sqlalchemy" in sys.modules:
        return

    class _Base:
        """Replacement for the declarative Base.

        Subclasses get a real __init__ that assigns keyword arguments to
        attributes, which is exactly the behaviour the code under test relies
        on when it constructs `WorkerPayout(...)`.
        """

        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

        def __getattr__(self, name: str) -> Any:
            # Unset mapped columns read as None, matching a fresh ORM
            # instance whose nullable columns have no value yet.
            if name.startswith("_"):
                raise AttributeError(name)
            return None

    _module("sqlalchemy", select=lambda *a, **k: _Sentinel(),
            func=_Sentinel(), delete=lambda *a, **k: _Sentinel(),
            text=lambda *a, **k: _Sentinel())
    _module("sqlalchemy.orm", Mapped=_Sentinel(),
            mapped_column=lambda *a, **k: _Sentinel(),
            relationship=lambda *a, **k: _Sentinel(),
            declarative_base=lambda **k: _Base)
    _module("sqlalchemy.ext")
    _module("sqlalchemy.ext.asyncio", AsyncSession=_Sentinel,
            create_async_engine=lambda *a, **k: _Sentinel(),
            async_sessionmaker=lambda *a, **k: _Sentinel())
    _module("sqlalchemy.dialects")
    _module("sqlalchemy.dialects.postgresql", ARRAY=_Sentinel,
            JSONB=_Sentinel, UUID=_Sentinel)
    _module("sqlalchemy.exc", IntegrityError=type("IntegrityError", (Exception,), {}))
    _module("sqlalchemy.pool", NullPool=_Sentinel)

    sys.modules["sqlalchemy"]._Base = _Base  # type: ignore[attr-defined]


def _install_pydantic() -> None:
    if "pydantic_settings" in sys.modules:
        return

    class BaseSettings:
        """Reads declared defaults off the class, ignoring the environment.

        The feature's settings all carry defaults, so this yields the same
        values a developer machine would load from a .env-less environment.
        """

        def __init__(self, **overrides: Any) -> None:
            for klass in reversed(type(self).__mro__):
                for key, value in vars(klass).items():
                    if key.startswith("_") or callable(value):
                        continue
                    if key in ("model_config",):
                        continue
                    # Skip descriptors (properties like cors_origin_list) —
                    # they compute from other fields and have no setter.
                    if isinstance(value, (property, classmethod, staticmethod)):
                        continue
                    setattr(self, key, value)
            # Required fields with no default (DATABASE_URL et al.) are never
            # touched by the code under test; give them harmless placeholders.
            for key, annotation in getattr(self, "__annotations__", {}).items():
                if not hasattr(self, key):
                    setattr(self, key, "")
            for key, value in overrides.items():
                setattr(self, key, value)

    _module("pydantic_settings", BaseSettings=BaseSettings,
            SettingsConfigDict=lambda **k: dict(**k))

    if "pydantic" not in sys.modules:
        class BaseModel:
            def __init__(self, **kwargs: Any) -> None:
                for key, value in kwargs.items():
                    setattr(self, key, value)

        _module("pydantic", BaseModel=BaseModel, Field=lambda *a, **k: None,
                field_validator=lambda *a, **k: (lambda f: f),
                ConfigDict=lambda **k: dict(**k))


def install() -> None:
    """Install every stub. Idempotent; safe to call from multiple modules."""
    _install_sqlalchemy()
    _install_pydantic()

    # app.core.database builds a real engine at import; replace it wholesale
    # since nothing under test touches a connection.
    if "app.core.database" not in sys.modules:
        base = sys.modules["sqlalchemy"]._Base  # type: ignore[attr-defined]
        _module("app.core.database", Base=base,
                get_db=lambda: None, engine=_Sentinel(),
                AsyncSessionLocal=_Sentinel())


# ===========================================================================
# Fakes used by the payout tests
# ===========================================================================
class FakeResult:
    """Mimics the slice of SQLAlchemy's Result the code under test uses."""

    def __init__(self, value: Any = None, many: list | None = None):
        self._value = value
        self._many = many or []

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list:
        return self._many

    def first(self) -> Any:
        return self._many[0] if self._many else None


class FakeSession:
    """An AsyncSession stand-in that returns queued results in order.

    Queries are matched positionally rather than by SQL, because the SQL is a
    sentinel here. Each test queues exactly the rows its code path will
    fetch, which keeps the fake honest: if the code makes an unexpected
    extra query, it gets None and the test notices.
    """

    def __init__(self, results: list | None = None):
        self._results = list(results or [])
        self.flushed = 0
        self.committed = 0
        self.added: list = []

    def queue(self, result: Any) -> None:
        self._results.append(result)

    async def execute(self, *_args: Any, **_kwargs: Any) -> FakeResult:
        if self._results:
            nxt = self._results.pop(0)
            return nxt if isinstance(nxt, FakeResult) else FakeResult(nxt)
        return FakeResult(None)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        pass

    def add(self, obj: Any) -> None:
        self.added.append(obj)
