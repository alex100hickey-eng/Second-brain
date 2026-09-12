"""Strategy modules. Each one: scan(ctx) -> [Signal]. The runner, risk manager and ledger do the rest."""
from .base import Signal, Strategy, size_for  # noqa: F401
