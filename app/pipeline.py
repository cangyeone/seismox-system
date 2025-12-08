"""Waveform processing pipeline with RNN-based picking support."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from dataclasses import dataclass

from typing import Dict, List, Optional

from sqlmodel import Session, select

from .database import engine
from .models import Event, PhasePick, Station, Waveform
from .pickers import run_phase_picker


@dataclass
class ProcessingRequest:
    waveform_id: int
    station_id: int
    file_path: str
    received_at: dt.datetime
    start_time: Optional[dt.datetime] = None
    samples: Optional[List[float]] = None
    sampling_rate: Optional[float] = None
    channel: Optional[str] = None


waveform_queue: asyncio.Queue[ProcessingRequest] = asyncio.Queue()
_station_buffers: Dict[str, Dict[str, object]] = {}


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
    picks = _buffer_and_pick(request)
    if not picks:
        _update_waveform_processed(request.waveform_id)
        return

    event = _associate_event(request.station_id, picks)
    _attach_picks_to_event(event.id, picks)
    _update_waveform_processed(request.waveform_id)


def _buffer_and_pick(request: ProcessingRequest) -> List[PhasePick]:
    """Accumulate per-station/channel buffers and run the neural picker every 10 seconds."""

    if not request.samples or not request.sampling_rate:
        return _simulate_phase_picks(request)

    key = f"{request.station_id}:{request.channel or 'UNK'}"
    buffer_state = _station_buffers.setdefault(
        key,
        {
            "start_time": request.start_time or request.received_at,
            "sampling_rate": request.sampling_rate,
            "samples": [],
        },
    )

    # reset sampling rate and base time if incoming stream changes
    buffer_state["sampling_rate"] = request.sampling_rate
    if not buffer_state.get("start_time"):
        buffer_state["start_time"] = request.start_time or request.received_at

    buffer_state["samples"].extend(request.samples)
    picks: List[PhasePick] = []
    block_len = int(buffer_state["sampling_rate"] * 10)

    while len(buffer_state["samples"]) >= block_len:
        start_time = buffer_state["start_time"] or request.received_at
        block = buffer_state["samples"][:block_len]
        picker_results = run_phase_picker(block, buffer_state["sampling_rate"])
        if picker_results:
            picks.extend(
                _convert_picker_results(
                    picker_results,
                    request.station_id,
                    buffer_state["sampling_rate"],
                    start_time,
                )
            )
        else:
            picks.extend(_simulate_phase_picks(request, base_time=start_time))

        # drop the processed block and advance buffer start time
        buffer_state["samples"] = buffer_state["samples"][block_len:]
        buffer_state["start_time"] = start_time + dt.timedelta(seconds=10)

    return picks


def _convert_picker_results(
    picker_results: List[tuple],
    station_id: int,
    sampling_rate: float,
    start_time: dt.datetime,
) -> List[PhasePick]:
    picks: List[PhasePick] = []
    phase_labels = ["Pg", "Sg", "Pn", "Sn"]
    for phase_idx, sample_idx, confidence in picker_results:
        if phase_idx is None:
            continue
        phase_idx_int = int(phase_idx)
        label = phase_labels[phase_idx_int] if 0 <= phase_idx_int < len(phase_labels) else f"phase{phase_idx_int}"
        pick_seconds = float(sample_idx) / sampling_rate
        pick_time = start_time + dt.timedelta(seconds=pick_seconds)
        picks.append(
            PhasePick(
                station_id=station_id,
                phase_type=label,
                pick_time=pick_time,
                quality=float(confidence),
                initial_motion=None,
                earthquake_type=None,
            )
        )
    return picks


def _simulate_phase_picks(request: ProcessingRequest, base_time: Optional[dt.datetime] = None) -> List[PhasePick]:
    phase_types = ["Pg", "Sg", "Pn", "Sn"]
    picks: List[PhasePick] = []
    for phase in phase_types:
        offset_seconds = random.uniform(0.5, 6.0)
        pick_time = (base_time or request.received_at) + dt.timedelta(seconds=offset_seconds)
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
