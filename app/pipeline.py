"""Simplified processing pipeline that simulates picking and association."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from dataclasses import dataclass
from typing import List

from sqlmodel import Session, select

from .database import engine
from .models import Event, PhasePick, Station, Waveform


@dataclass
class ProcessingRequest:
    waveform_id: int
    station_id: int
    file_path: str
    received_at: dt.datetime


waveform_queue: asyncio.Queue[ProcessingRequest] = asyncio.Queue()


async def enqueue_waveform(request: ProcessingRequest) -> None:
    await waveform_queue.put(request)


async def process_waveforms() -> None:
    while True:
        request = await waveform_queue.get()
        try:
            await _handle_request(request)
        finally:
            waveform_queue.task_done()


async def _handle_request(request: ProcessingRequest) -> None:
    picks = _simulate_phase_picks(request)
    event = _associate_event(request.station_id, picks)
    _attach_picks_to_event(event.id, picks)
    _update_waveform_processed(request.waveform_id)


def _simulate_phase_picks(request: ProcessingRequest) -> List[PhasePick]:
    phase_types = ["Pg", "Sg", "Pn", "Sn"]
    picks: List[PhasePick] = []
    for phase in phase_types:
        offset_seconds = random.uniform(0.5, 6.0)
        pick_time = request.received_at + dt.timedelta(seconds=offset_seconds)
        picks.append(
            PhasePick(
                station_id=request.station_id,
                phase_type=phase,
                pick_time=pick_time,
                quality=random.uniform(0.7, 0.99),
                initial_motion=random.choice(["up", "down"]),
                earthquake_type=random.choice(["tectonic", "explosion", "volcanic"]),
            )
        )
    return picks


def _associate_event(station_id: int, picks: List[PhasePick]) -> Event:
    with Session(engine) as session:
        station = session.exec(select(Station).where(Station.id == station_id)).one()
        origin_time = min(p.pick_time for p in picks)
        latitude = station.latitude + random.uniform(-0.05, 0.05)
        longitude = station.longitude + random.uniform(-0.05, 0.05)
        depth_km = abs(random.gauss(10, 4))
        magnitude = round(random.uniform(1.5, 4.5), 2)
        event = Event(
            origin_time=origin_time,
            latitude=latitude,
            longitude=longitude,
            depth_km=depth_km,
            magnitude=magnitude,
            event_type="earthquake",
            preferred_station_id=station.id,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event


def _attach_picks_to_event(event_id: int, picks: List[PhasePick]) -> None:
    with Session(engine) as session:
        for pick in picks:
            pick.event_id = event_id
            session.add(pick)
        session.commit()


def _update_waveform_processed(waveform_id: int) -> None:
    with Session(engine) as session:
        waveform = session.get(Waveform, waveform_id)
        if waveform:
            waveform.processed = True
            session.add(waveform)
            session.commit()
