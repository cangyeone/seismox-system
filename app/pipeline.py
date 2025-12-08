"""Waveform processing pipeline with RNN-based picking support."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from dataclasses import dataclass

from typing import Callable, Dict, List, Optional

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
_station_buffers: Dict[int, Dict[str, object]] = {}
_pick_listeners: List[Callable[[int, List[PhasePick]], None]] = []


async def enqueue_waveform(request: ProcessingRequest) -> None:
    await waveform_queue.put(request)


def register_pick_listener(listener: Callable[[int, List[PhasePick]], None]) -> None:
    """Allow other modules (e.g., live visualization) to receive pick updates."""

    _pick_listeners.append(listener)


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

    _notify_pick_listeners(request.station_id, picks)
    event = _associate_event(request.station_id, picks)
    _attach_picks_to_event(event.id, picks)
    _update_waveform_processed(request.waveform_id)


def _buffer_and_pick(request: ProcessingRequest) -> List[PhasePick]:
    """Accumulate per-station/channel buffers and run the neural picker every 10 seconds."""

    if not request.samples or not request.sampling_rate:
        return _simulate_phase_picks(request)

    station_buf = _station_buffers.setdefault(
        request.station_id,
        {
            "channels": {},
        },
    )

    channels: Dict[str, Dict[str, object]] = station_buf["channels"]  # type: ignore[assignment]
    chan_buf = channels.setdefault(
        request.channel or "UNK",
        {
            "start_time": request.start_time or request.received_at,
            "sampling_rate": request.sampling_rate,
            "samples": [],
        },
    )

    # reset sampling rate and base time if incoming stream changes
    chan_buf["sampling_rate"] = request.sampling_rate
    if not chan_buf.get("start_time"):
        chan_buf["start_time"] = request.start_time or request.received_at

    chan_buf["samples"].extend(request.samples)
    picks: List[PhasePick] = []

    # Use the smallest available channel length to ensure aligned three-component blocks
    block_len = int(request.sampling_rate * 10)
    while True:
        if not channels:
            break
        min_len = min(len(buf["samples"]) for buf in channels.values())
        if min_len < block_len:
            break

        # choose up to three channels deterministically
        selected_names = sorted(channels.keys())[:3]
        sample_matrix: List[List[float]] = []
        start_times = []
        for name in selected_names:
            buf = channels[name]
            start_times.append(buf.get("start_time") or request.received_at)
            sample_matrix.append(buf["samples"][:block_len])

        # pad/duplicate channels to reach three components
        while len(sample_matrix) < 3:
            sample_matrix.append(list(sample_matrix[0]))

        start_time = min(start_times)
        picker_results = run_phase_picker(sample_matrix, request.sampling_rate)
        if picker_results:
            picks.extend(
                _convert_picker_results(
                    picker_results,
                    request.station_id,
                    request.sampling_rate,
                    start_time,
                )
            )
        else:
            picks.extend(_simulate_phase_picks(request, base_time=start_time))

        # drop processed samples and advance start times per channel
        for name in selected_names:
            buf = channels[name]
            buf["samples"] = buf["samples"][block_len:]
            buf["start_time"] = (buf.get("start_time") or start_time) + dt.timedelta(seconds=10)

    return picks


def _notify_pick_listeners(station_id: int, picks: List[PhasePick]) -> None:
    if not _pick_listeners or not picks:
        return

    for listener in _pick_listeners:
        try:
            listener(station_id, picks)
        except Exception:
            # keep pipeline alive even if downstream listeners fail
            continue


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
