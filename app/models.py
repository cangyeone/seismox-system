"""SQLModel entities used throughout the SeismoX prototype."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


class StationBase(SQLModel):
    code: str = Field(index=True, unique=True, max_length=32)
    name: str
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    is_active: bool = True
    status: str = "healthy"


class Station(StationBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    waveforms: List["Waveform"] = Relationship(back_populates="station")
    picks: List["PhasePick"] = Relationship(back_populates="station")
    events: List["Event"] = Relationship(back_populates="preferred_station")


class StationRead(StationBase):
    id: int


class WaveformBase(SQLModel):
    station_id: int = Field(foreign_key="station.id")
    file_path: str
    received_at: dt.datetime = Field(default_factory=dt.datetime.utcnow, index=True)
    processed: bool = False


class Waveform(WaveformBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    station: Station = Relationship(back_populates="waveforms")


class PhasePickBase(SQLModel):
    station_id: int = Field(foreign_key="station.id")
    event_id: Optional[int] = Field(default=None, foreign_key="event.id")
    phase_type: str
    pick_time: dt.datetime
    quality: float = 0.0
    initial_motion: Optional[str] = None
    earthquake_type: Optional[str] = None


class PhasePick(PhasePickBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    station: Station = Relationship(back_populates="picks")
    event: Optional["Event"] = Relationship(back_populates="picks")


class EventBase(SQLModel):
    origin_time: dt.datetime
    latitude: float
    longitude: float
    depth_km: float = 10.0
    magnitude: float = 0.0
    event_type: str = "earthquake"
    preferred_station_id: Optional[int] = Field(default=None, foreign_key="station.id")


class Event(EventBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    picks: List[PhasePick] = Relationship(back_populates="event")
    preferred_station: Optional[Station] = Relationship(back_populates="events")


class EventWithPicks(EventBase):
    id: int
    picks: List[PhasePick]


class HealthResponse(SQLModel):
    status: str
    message: str
    processing_queue_size: int
