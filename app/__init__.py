"""App package initialization and compatibility shims."""

from __future__ import annotations

import sqlalchemy.types as _satypes
from sqlalchemy.orm import RelationshipProperty as _RelationshipProperty

# SQLModel 0.0.16 expects SQLAlchemy to expose DOUBLE / Double and
# DOUBLE_PRECISION, which were removed in 2.x. Provide lightweight aliases when
# running against newer SQLAlchemy builds so imports like
# ``from sqlalchemy.types import DOUBLE`` / ``Double`` or ``DOUBLE_PRECISION``
# succeed without pinning.
if not hasattr(_satypes, "DOUBLE"):
    _satypes.DOUBLE = _satypes.Float

if not hasattr(_satypes, "Double"):
    _satypes.Double = _satypes.Float

if not hasattr(_satypes, "DOUBLE_PRECISION"):
    _satypes.DOUBLE_PRECISION = _satypes.Float

# SQLModel also imports UUID from sqlalchemy.types; newer SQLAlchemy versions
# no longer expose it at that location. Re-export a compatible type so imports
# keep working across versions.
if not hasattr(_satypes, "UUID"):
    if hasattr(_satypes, "Uuid"):
        _satypes.UUID = _satypes.Uuid
    else:
        _satypes.UUID = _satypes.CHAR

# Some SQLModel builds import ``Uuid`` directly; if it's missing (common on
# SQLAlchemy 2.x), provide a CHAR-based fallback so imports succeed.
if not hasattr(_satypes, "Uuid"):
    _satypes.Uuid = _satypes.CHAR

# SQLAlchemy 1.4's ``RelationshipProperty`` is not subscriptable, but SQLModel
# 0.0.16 annotates it using ``RelationshipProperty[Any]``. Provide a minimal
# ``__class_getitem__`` so the import layer can treat it like a generic without
# raising ``TypeError`` when SQLAlchemy is older than 2.x.
if not hasattr(_RelationshipProperty, "__class_getitem__"):
    _RelationshipProperty.__class_getitem__ = classmethod(lambda cls, _: cls)
