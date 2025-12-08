"""IRIS FDSN/plot helpers used by the dashboard and APIs."""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
from io import StringIO
from typing import List, Tuple

import httpx


class IrisStation:
    def __init__(self, network: str, code: str, latitude: float, longitude: float, elevation_m: float, name: str):
        self.network = network
        self.code = code
        self.latitude = latitude
        self.longitude = longitude
        self.elevation_m = elevation_m
        self.name = name

    def dict(self) -> dict:
        return {
            "network": self.network,
            "code": self.code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "elevation_m": self.elevation_m,
            "name": self.name,
        }


async def fetch_station_catalog(network: str = "IU", limit: int = 12) -> List[IrisStation]:
    """Fetch station metadata from IRIS in GeoCSV format and return unique stations."""

    url = "https://service.iris.edu/fdsnws/station/1/query"
    params = {"network": network, "level": "station", "format": "geocsv"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()

    text = resp.text
    reader = csv.reader(StringIO(text))
    stations: dict[str, IrisStation] = {}

    header_seen = False
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        if not header_seen:
            header_seen = True
            continue
        try:
            net, sta, _loc, _cha, lat, lon, elev, *_rest = row
        except ValueError:
            continue
        if sta in stations:
            continue
        stations[sta] = IrisStation(
            network=net,
            code=sta,
            latitude=float(lat),
            longitude=float(lon),
            elevation_m=float(elev),
            name=f"{net}-{sta}"
        )
        if len(stations) >= limit:
            break

    return list(stations.values())


async def fetch_waveform_plot(
    network: str,
    station: str,
    location: str = "00",
    channel: str = "BHZ",
    duration_seconds: int = 300,
) -> Tuple[str, str]:
    """Fetch a waveform plot PNG from IRIS timeseries service and return base64 + content type."""

    endtime = dt.datetime.utcnow()
    starttime = endtime - dt.timedelta(seconds=duration_seconds)
    url = "https://service.iris.edu/irisws/timeseries/1/query"
    params = {
        "net": network,
        "sta": station,
        "loc": location,
        "cha": channel,
        "starttime": starttime.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": endtime.strftime("%Y-%m-%dT%H:%M:%S"),
        "output": "plot",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "image/png")
    encoded = base64.b64encode(resp.content).decode()
    return encoded, content_type


def fetch_waveform_plot_sync(
    network: str,
    station: str,
    location: str = "00",
    channel: str = "BHZ",
    duration_seconds: int = 300,
) -> Tuple[str, str]:
    return asyncio.get_event_loop().run_until_complete(
        fetch_waveform_plot(network, station, location, channel, duration_seconds)
    )
