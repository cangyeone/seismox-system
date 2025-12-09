"""App package initialization and compatibility shims."""

from __future__ import annotations

import sqlalchemy.types as _satypes

# SQLModel 0.0.16 expects SQLAlchemy to expose DOUBLE and DOUBLE_PRECISION,
# which were removed in 2.x. Provide lightweight aliases when running against
# newer SQLAlchemy builds so imports like ``from sqlalchemy.types import DOUBLE``
# or ``DOUBLE_PRECISION`` succeed without pinning.
if not hasattr(_satypes, "DOUBLE"):
    _satypes.DOUBLE = _satypes.Float

if not hasattr(_satypes, "DOUBLE_PRECISION"):
    _satypes.DOUBLE_PRECISION = _satypes.Float
