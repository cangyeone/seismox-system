from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import Any, Dict, Optional, Set

import httpx
from sqlmodel import Session, select

from .database import engine
from .models import Event, PhasePick, Station, Waveform

USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_EVENT_PREFIX = "usgs:"

_poll_task: Optional[asyncio.Task] = None
_last_fetch: Optional[dt.datetime] = None
_last_error: Optional[str] = None
_seen_event_ids: Set[str] = set()


async def start_usgs_stream(interval_seconds: int = 60) -> bool:
    global _poll_task
    if _poll_task and not _poll_task.done():
        return False
    _poll_task = asyncio.create_task(_poll_loop(interval_seconds))
    return True


async def stop_usgs_stream() -> bool:
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    stopped = _poll_task is None or _poll_task.done()
    _poll_task = None
    return stopped


def get_usgs_status() -> Dict[str, Any]:
    return {
        "running": _poll_task is not None and not _poll_task.done(),
        "last_fetch": _last_fetch.isoformat() if _last_fetch else None,
        "last_error": _last_error,
        "events_seen": len(_seen_event_ids),
        "feed": USGS_FEED_URL,
    }


async def _poll_loop(interval_seconds: int) -> None:
    global _last_fetch, _last_error
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                await _pull_once(client)
                _last_error = None
            except Exception as exc:  # pragma: no cover - best effort fetch
                _last_error = str(exc)
            _last_fetch = dt.datetime.utcnow()
            await asyncio.sleep(interval_seconds)


async def _pull_once(client: httpx.AsyncClient) -> None:
    resp = await client.get(USGS_FEED_URL)
    resp.raise_for_status()
    payload = resp.json()
    features = payload.get("features", [])
    for feature in features:
        usgs_id = feature.get("id")
        if not usgs_id or usgs_id in _seen_event_ids:
            continue
        if _event_exists(usgs_id):
            _seen_event_ids.add(usgs_id)
            continue
        _materialize_usgs_event(usgs_id, feature)
        _seen_event_ids.add(usgs_id)


def _event_exists(usgs_id: str) -> bool:
    with Session(engine) as session:
        existing = session.exec(
            select(Event).where(Event.event_type == f"{USGS_EVENT_PREFIX}{usgs_id}")
        ).first()
        return existing is not None


def _materialize_usgs_event(usgs_id: str, feature: Dict[str, Any]) -> None:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or [None, None, None]
    lon, lat, depth_km = coords[0], coords[1], coords[2]
    origin_ms = properties.get("time")
    origin_time = (
        dt.datetime.utcfromtimestamp(origin_ms / 1000) if origin_ms else dt.datetime.utcnow()
    )
    magnitude = properties.get("mag") or 0.0
    depth_km = abs(depth_km) if depth_km is not None else 10.0

    with Session(engine) as session:
        station = _ensure_usgs_station(session)
        event = Event(
            origin_time=origin_time,
            latitude=lat or 0.0,
            longitude=lon or 0.0,
            depth_km=depth_km,
            magnitude=float(magnitude),
            event_type=f"{USGS_EVENT_PREFIX}{usgs_id}",
            preferred_station_id=station.id,
        )
        session.add(event)
        session.commit()
        session.refresh(event)

        picks = _virtual_picks(event, station.id)
        for pick in picks:
            session.add(pick)

        waveform = Waveform(
            station_id=station.id,
            file_path=f"{USGS_EVENT_PREFIX}{usgs_id}",
            received_at=origin_time,
            processed=True,
        )
        session.add(waveform)
        session.commit()


def _ensure_usgs_station(session: Session) -> Station:
    station = session.exec(select(Station).where(Station.code == "USGS"))
    station = station.first() if station is not None else None
    if station:
        return station

    station = Station(
        code="USGS",
        name="USGS Virtual Network",
        latitude=0.0,
        longitude=0.0,
        elevation_m=0.0,
        is_active=True,
        status="virtual",
    )
    session.add(station)
    session.commit()
    session.refresh(station)
    return station


def _virtual_picks(event: Event, station_id: int) -> list[PhasePick]:
    phase_types = ["Pg", "Sg", "Pn", "Sn"]
    picks: list[PhasePick] = []
    for idx, phase in enumerate(phase_types):
        picks.append(
            PhasePick(
                station_id=station_id,
                event_id=event.id,
                phase_type=phase,
                pick_time=event.origin_time + dt.timedelta(seconds=idx + 1),
                quality=random.uniform(0.75, 0.98),
                initial_motion=random.choice(["up", "down"]),
                earthquake_type="usgs-feed",
            )
        )
    return picks
