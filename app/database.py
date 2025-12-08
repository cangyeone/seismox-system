from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel, create_engine

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATA_DIR / "catalog.db"

engine = create_engine(f"sqlite:///{DATABASE_PATH}", echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
