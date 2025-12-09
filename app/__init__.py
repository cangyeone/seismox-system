"""App package initialization and compatibility shims."""

from __future__ import annotations

import sqlalchemy.types as _satypes

# SQLModel 0.0.16 expects SQLAlchemy to expose DOUBLE, which was removed in 2.x.
# Provide a lightweight alias when running against newer SQLAlchemy builds so
# imports like ``from sqlalchemy.types import DOUBLE`` succeed without pinning.
if not hasattr(_satypes, "DOUBLE"):
    _satypes.DOUBLE = _satypes.Float
