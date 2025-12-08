from __future__ import annotations

import asyncio
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlmodel import SQLModel, Session, select

from .database import engine, init_db
from .iris import fetch_station_catalog, fetch_waveform_plot
from .iris_stream import (
    get_latest_frame,
    get_live_status,
    start_live_stream,
    stop_live_stream,
)
from .models import Event, EventWithPicks, HealthResponse, PhasePick, Station, StationRead, Waveform
from .pipeline import ProcessingRequest, enqueue_waveform, process_waveforms, waveform_queue
from .storage import persist_waveform
from .usgs import get_usgs_status, start_usgs_stream, stop_usgs_stream

app = FastAPI(title="SeismoX System", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    loop = asyncio.get_event_loop()
    loop.create_task(process_waveforms())


def get_session():
    with Session(engine) as session:
        yield session


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        message="SeismoX pipeline online",
        processing_queue_size=waveform_queue.qsize(),
    )


@app.post("/stations", response_model=StationRead)
def create_station(station: Station, session: Session = Depends(get_session)) -> Station:
    session.add(station)
    session.commit()
    session.refresh(station)
    return station


@app.get("/stations", response_model=List[StationRead])
def list_stations(session: Session = Depends(get_session)) -> List[StationRead]:
    return session.exec(select(Station)).all()


@app.get("/stations/{station_id}", response_model=StationRead)
def get_station(station_id: int, session: Session = Depends(get_session)) -> StationRead:
    station = session.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


@app.post("/stations/import/iris")
async def import_iris_stations(network: str = "IU", limit: int = 12, session: Session = Depends(get_session)) -> dict:
    catalog = await fetch_station_catalog(network=network, limit=limit)
    created = 0
    for st in catalog:
        existing = session.exec(select(Station).where(Station.code == st.code)).first()
        if existing:
            continue
        station = Station(
            code=st.code,
            name=st.name,
            latitude=st.latitude,
            longitude=st.longitude,
            elevation_m=st.elevation_m,
            status="healthy",
        )
        session.add(station)
        created += 1
    session.commit()
    return {"imported": created, "total_available": len(catalog)}


class WaveformIngestRequest(SQLModel):
    station_code: str
    payload_base64: str


@app.post("/waveforms/ingest")
async def ingest_waveform(request: WaveformIngestRequest, session: Session = Depends(get_session)) -> dict:
    station = session.exec(select(Station).where(Station.code == request.station_code)).first()
    if not station:
        raise HTTPException(status_code=404, detail="Station not registered")

    file_path, received_at = persist_waveform(request.station_code, request.payload_base64)
    waveform = Waveform(
        station_id=station.id,
        file_path=file_path,
        received_at=received_at,
    )
    session.add(waveform)
    session.commit()
    session.refresh(waveform)

    await enqueue_waveform(
        ProcessingRequest(
            waveform_id=waveform.id,
            station_id=station.id,
            file_path=file_path,
            received_at=received_at,
        )
    )
    return {"waveform_id": waveform.id, "queued": True}


@app.get("/events", response_model=List[Event])
def list_events(session: Session = Depends(get_session)) -> List[Event]:
    return session.exec(select(Event).order_by(Event.origin_time.desc())).all()


@app.get("/events/{event_id}", response_model=EventWithPicks)
def get_event(event_id: int, session: Session = Depends(get_session)) -> EventWithPicks:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    picks = session.exec(select(PhasePick).where(PhasePick.event_id == event.id)).all()
    return EventWithPicks(**event.dict(), id=event.id, picks=picks)


@app.get("/picks", response_model=List[PhasePick])
def list_picks(session: Session = Depends(get_session)) -> List[PhasePick]:
    return session.exec(select(PhasePick).order_by(PhasePick.pick_time.desc()).limit(200)).all()


@app.get("/iris/stations")
async def iris_stations(network: str = "IU", limit: int = 12) -> dict:
    catalog = await fetch_station_catalog(network=network, limit=limit)
    return {"network": network, "stations": [s.dict() for s in catalog]}


@app.get("/iris/waveform")
async def iris_waveform(
    network: str = "IU", station: str = "ANMO", location: str = "00", channel: str = "BHZ", duration: int = 300
) -> dict:
    image_b64, content_type = await fetch_waveform_plot(network, station, location, channel, duration)
    return {
        "image_base64": image_b64,
        "content_type": content_type,
        "network": network,
        "station": station,
        "channel": channel,
        "duration": duration,
    }


@app.post("/iris/live/start")
async def iris_live_start(
    network: str = "IU", station: str = "ANMO", location: str = "00", channel: str = "BHZ"
) -> dict:
    started = await start_live_stream(network=network, station=station, location=location, channel=channel)
    status = get_live_status()
    status.update({"started": started})
    return status


@app.post("/iris/live/stop")
async def iris_live_stop() -> dict:
    stopped = await stop_live_stream()
    status = get_live_status()
    status.update({"stopped": stopped})
    return status


@app.get("/iris/live/status")
async def iris_live_status() -> dict:
    return get_live_status()


@app.get("/iris/live/frame")
async def iris_live_frame() -> dict:
    frame = get_latest_frame()
    if not frame:
        raise HTTPException(status_code=404, detail="暂无实时波形")
    return frame


@app.get("/usgs/status")
async def usgs_status() -> dict:
    return get_usgs_status()


@app.post("/usgs/start")
async def start_usgs() -> dict:
    started = await start_usgs_stream()
    return {"running": get_usgs_status()["running"], "started": started}


@app.post("/usgs/stop")
async def stop_usgs() -> dict:
    stopped = await stop_usgs_stream()
    return {"running": get_usgs_status()["running"], "stopped": stopped}


DASHBOARD_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>SeismoX 控制台</title>
  <style>
    :root { font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #0f172a; background: #f8fafc; }
    body { margin: 0; padding: 0; }
    header { background: linear-gradient(120deg, #0ea5e9, #6366f1); color: white; padding: 16px 24px; }
    .container { max-width: 1100px; margin: 0 auto; padding: 16px; }
    h1, h2, h3 { margin: 0 0 8px 0; }
    section { background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; margin-bottom: 16px; box-shadow: 0 8px 20px rgba(15,23,42,0.04); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
    label { display: block; font-weight: 600; margin-top: 8px; }
    input, select { width: 100%; padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; }
    button { background: #0ea5e9; color: white; border: none; padding: 10px 14px; border-radius: 6px; cursor: pointer; font-weight: 700; }
    button.secondary { background: #e2e8f0; color: #0f172a; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px; border-bottom: 1px solid #e2e8f0; text-align: left; }
    .pill { display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 999px; font-size: 12px; background: #e0f2fe; color: #0369a1; }
    .pill.ok { background: #dcfce7; color: #15803d; }
    .pill.warn { background: #fff7ed; color: #c2410c; }
    .muted { color: #64748b; font-size: 14px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .card { border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; background: #f8fafc; }
    .waveform-box { background: #0b1221; border-radius: 10px; padding: 12px; text-align: center; }
    .waveform-box img { max-width: 100%; border-radius: 8px; }
    .live-box { background: #0b1221; border-radius: 10px; padding: 12px; color: #e2e8f0; }
    canvas { width: 100%; max-width: 100%; border-radius: 8px; background: #020617; }
  </style>
</head>
<body>
  <header>
    <div class=\"container\">
      <h1>SeismoX 编目控制台</h1>
      <p>实时台站可视化、USGS 数据演示接入、事件与震相浏览。</p>
      <p class=\"muted\">API 文档: <a style=\"color:white;font-weight:700;text-decoration:underline;\" href=\"/docs\">/docs</a></p>
    </div>
  </header>
  <div class=\"container\">
    <section id=\"health\">
      <div class=\"row\">
        <h2>运行状态</h2>
        <span id=\"health-pill\" class=\"pill\">加载中...</span>
      </div>
      <p class=\"muted\" id=\"health-message\"></p>
      <p class=\"muted\" id=\"queue-size\"></p>
    </section>

    <section id=\"sources\">
      <div class=\"row\">
        <h2>数据源 / 方法管理</h2>
        <span class=\"pill\">实时处理</span>
      </div>
      <div class=\"grid\">
        <div>
          <h3>USGS 实时数据流</h3>
          <p class=\"muted\">拉取官方 GeoJSON 实时地震目录并生成虚拟震相/事件，便于演示界面联动。</p>
          <div class=\"row\" style=\"margin-top:8px;\">
            <button id=\"start-usgs\">启动接入</button>
            <button id=\"stop-usgs\" class=\"secondary\">停止</button>
          </div>
          <p class=\"muted\" id=\"usgs-status\"></p>
        </div>
        <div>
          <h3>本地实时处理</h3>
          <p class=\"muted\">通过 <code>/waveforms/ingest</code> 将实时波形推送到队列，由后台处理器拾取 Pg/Sg/Pn/Sn 并关联事件。</p>
          <p class=\"muted\">可结合 Kafka/Flink/Spark 生产消费链路替换当前内存队列。</p>
        </div>
        <div>
          <h3>IRIS 实时波形演示</h3>
          <p class=\"muted\">自动获取 IRIS FDSN 台站列表并从 timeseries 服务抓取最新波形图像，作为实时展示基线。</p>
          <p class=\"muted\">下方“实时波形”区域会定时刷新选中台站的波形。</p>
        </div>
      </div>
    </section>

    <section id=\"waveforms\">
      <div class=\"row\" style=\"justify-content: space-between;\">
        <div>
          <h2>IRIS 实时波形</h2>
          <p class=\"muted\">选择远程台站并查看最近 5 分钟的波形截图。</p>
        </div>
        <div class=\"row\"> 
          <label for=\"iris-station-select\" style=\"margin:0; font-weight:600;\">台站</label>
          <select id=\"iris-station-select\"></select>
          <button id=\"wf-refresh\" class=\"secondary\">刷新</button>
        </div>
      </div>
      <div class=\"waveform-box\">
        <img id=\"wf-img\" alt=\"waveform preview\" />
        <p class=\"muted\" id=\"wf-meta\"></p>
        <p class=\"muted\" id=\"wf-error\" style=\"color:#fbbf24;\"></p>
      </div>
    </section>

    <section id=\"seedlink\">
      <div class=\"row\" style=\"justify-content: space-between;\">
        <div>
          <h2>IRIS SeedLink 实时流</h2>
          <p class=\"muted\">使用 obspy SeedLink 客户端持续接收 IU.ANMO.00.BHZ，实时入库并绘制波形。</p>
        </div>
        <div class=\"row\">
          <button id=\"seedlink-start\">启动实时流</button>
          <button id=\"seedlink-stop\" class=\"secondary\">停止</button>
        </div>
      </div>
      <p class=\"muted\" id=\"seedlink-status\">尚未启动</p>
      <div class=\"live-box\">
        <canvas id=\"live-canvas\" width=\"960\" height=\"220\"></canvas>
        <p class=\"muted\" id=\"live-meta\"></p>
        <p class=\"muted\" id=\"live-error\" style=\"color:#fbbf24;\"></p>
      </div>
    </section>

    <section id=\"stations\">
      <h2>台站管理</h2>
      <div class=\"grid\">
        <div>
          <h3>新增台站</h3>
          <label>台站代码</label><input id=\"st-code\" placeholder=\"ABC1\" />
          <label>名称</label><input id=\"st-name\" placeholder=\"Demo Station\" />
          <label>纬度</label><input id=\"st-lat\" type=\"number\" step=\"0.0001\" />
          <label>经度</label><input id=\"st-lon\" type=\"number\" step=\"0.0001\" />
          <label>高程 (m)</label><input id=\"st-ele\" type=\"number\" step=\"0.1\" />
          <label>状态</label><input id=\"st-status\" placeholder=\"healthy\" />
          <div style=\"margin-top:10px;\"><button id=\"st-submit\">保存台站</button></div>
          <p class=\"muted\" id=\"st-message\"></p>
        </div>
        <div>
          <h3>已注册台站</h3>
          <table id=\"st-table\">
            <thead><tr><th>代码</th><th>名称</th><th>位置</th><th>状态</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div>
          <h3>自动获取 (IRIS)</h3>
          <p class=\"muted\">点击一键导入，快速把 IRIS 台站写入本地数据库。</p>
          <div class=\"row\" style=\"margin-bottom:8px;\">
            <button id=\"import-iris\">导入 IU 网络</button>
            <button id=\"refresh-iris\" class=\"secondary\">仅刷新列表</button>
          </div>
          <table id=\"iris-table\">
            <thead><tr><th>网络</th><th>代码</th><th>位置</th></tr></thead>
            <tbody></tbody>
          </table>
          <p class=\"muted\" id=\"iris-message\"></p>
        </div>
      </div>
    </section>

    <section id=\"events\">
      <h2>事件 / 震相</h2>
      <p class=\"muted\">展示最新事件（USGS 或实时处理生成）及其 Pg/Sg/Pn/Sn 拾取。</p>
      <table>
        <thead>
          <tr><th>ID</th><th>类型</th><th>时间</th><th>位置</th><th>M</th><th>优选台站</th></tr>
        </thead>
        <tbody id=\"event-rows\"></tbody>
      </table>
    </section>
  </div>

  <script>
    async function refreshHealth() {
      const res = await fetch('/health');
      const data = await res.json();
      const pill = document.getElementById('health-pill');
      pill.textContent = data.status;
      pill.className = 'pill ok';
      document.getElementById('health-message').textContent = data.message;
      document.getElementById('queue-size').textContent = `处理队列: ${data.processing_queue_size}`;
    }

    async function refreshStations() {
      const res = await fetch('/stations');
      const data = await res.json();
      const tbody = document.querySelector('#st-table tbody');
      tbody.innerHTML = '';
      data.forEach(st => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${st.code}</td><td>${st.name}</td><td>${st.latitude.toFixed(3)}, ${st.longitude.toFixed(3)}</td><td><span class="pill">${st.status}</span></td>`;
        tbody.appendChild(tr);
      });
    }

    let irisStationCache = [];

    async function refreshIrisStations() {
      const res = await fetch('/iris/stations?network=IU&limit=12');
      const data = await res.json();
      irisStationCache = data.stations || [];
      const tbody = document.querySelector('#iris-table tbody');
      const select = document.getElementById('iris-station-select');
      tbody.innerHTML = '';
      select.innerHTML = '';
      irisStationCache.forEach((st, idx) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${st.network}</td><td>${st.code}</td><td>${st.latitude.toFixed(2)}, ${st.longitude.toFixed(2)}</td>`;
        tbody.appendChild(tr);
        const opt = document.createElement('option');
        opt.value = st.code;
        opt.dataset.network = st.network;
        opt.textContent = `${st.code} (${st.network})`;
        if (idx === 0) opt.selected = true;
        select.appendChild(opt);
      });
      if (!irisStationCache.length) {
        document.getElementById('iris-message').textContent = '未获取到台站，稍后重试';
      } else {
        document.getElementById('iris-message').textContent = `共 ${irisStationCache.length} 个台站`;
      }
    }

    async function refreshEvents() {
      const res = await fetch('/events');
      const data = await res.json();
      const tbody = document.getElementById('event-rows');
      tbody.innerHTML = '';
      data.forEach(ev => {
        const row = document.createElement('tr');
        const type = ev.event_type && ev.event_type.startsWith('usgs:') ? 'USGS' : ev.event_type;
        const origin = new Date(ev.origin_time).toLocaleString();
        row.innerHTML = `<td>${ev.id}</td><td>${type}</td><td>${origin}</td><td>${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)}</td><td>${ev.magnitude.toFixed(2)}</td><td>${ev.preferred_station_id ?? '-'}</td>`;
        tbody.appendChild(row);
      });
    }

    async function refreshWaveform() {
      const select = document.getElementById('iris-station-select');
      if (!select || !select.value) return;
      const station = select.value;
      const network = select.selectedOptions[0]?.dataset.network || 'IU';
      try {
        const res = await fetch(`/iris/waveform?network=${network}&station=${station}&channel=BHZ&duration=300`);
        if (!res.ok) {
          document.getElementById('wf-error').textContent = '波形获取失败';
          return;
        }
        const data = await res.json();
        document.getElementById('wf-img').src = `data:${data.content_type};base64,${data.image_base64}`;
        document.getElementById('wf-meta').textContent = `${data.network}.${data.station}.${data.channel} 最近 ${Math.round(data.duration/60)} 分钟`;
        document.getElementById('wf-error').textContent = '';
      } catch (err) {
        document.getElementById('wf-error').textContent = '波形获取失败';
      }
    }

    let seedlinkRunning = false;

    function drawLiveWaveform(samples) {
      const canvas = document.getElementById('live-canvas');
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (!samples || !samples.length) return;
      const min = Math.min(...samples);
      const max = Math.max(...samples);
      const range = max - min || 1;
      ctx.strokeStyle = '#22d3ee';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      samples.forEach((v, idx) => {
        const x = (idx / (samples.length - 1)) * canvas.width;
        const y = canvas.height - ((v - min) / range) * canvas.height;
        if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    async function refreshSeedlinkStatus() {
      const res = await fetch('/iris/live/status');
      const data = await res.json();
      seedlinkRunning = data.running;
      const detail = data.running
        ? `${data.network}.${data.station}.${data.channel} | 已接收 ${data.frames} 段 | 最近: ${data.last_frame || '未就绪'}`
        : '尚未运行';
      document.getElementById('seedlink-status').textContent = detail;
      if (data.error) {
        document.getElementById('live-error').textContent = data.error;
      }
    }

    async function refreshLiveFrame() {
      if (!seedlinkRunning) {
        return;
      }
      try {
        const res = await fetch('/iris/live/frame');
        if (!res.ok) return;
        const data = await res.json();
        drawLiveWaveform(data.samples || []);
        document.getElementById('live-meta').textContent = `${data.network}.${data.station}.${data.channel} @ ${data.sampling_rate.toFixed(2)} Hz | ${data.start_time}`;
        document.getElementById('live-error').textContent = '';
      } catch (err) {
        document.getElementById('live-error').textContent = '实时波形刷新失败';
      }
    }

    async function refreshUSGS() {
      const res = await fetch('/usgs/status');
      const data = await res.json();
      const status = data.running ? '运行中' : '已停止';
      const detail = data.last_fetch ? `最近拉取: ${new Date(data.last_fetch).toLocaleTimeString()} | 已接收 ${data.events_seen} 条` : '尚未拉取';
      document.getElementById('usgs-status').textContent = `${status} – ${detail}`;
    }

    document.getElementById('st-submit').onclick = async () => {
      const payload = {
        code: document.getElementById('st-code').value,
        name: document.getElementById('st-name').value,
        latitude: Number(document.getElementById('st-lat').value),
        longitude: Number(document.getElementById('st-lon').value),
        elevation_m: Number(document.getElementById('st-ele').value || 0),
        status: document.getElementById('st-status').value || 'healthy'
      };
      const res = await fetch('/stations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (res.ok) {
        document.getElementById('st-message').textContent = '保存成功';
        refreshStations();
      } else {
        const err = await res.json();
        document.getElementById('st-message').textContent = '错误: ' + (err.detail || '无法保存');
      }
    };

    document.getElementById('start-usgs').onclick = async () => {
      await fetch('/usgs/start', { method: 'POST' });
      refreshUSGS();
      refreshEvents();
    };
    document.getElementById('stop-usgs').onclick = async () => {
      await fetch('/usgs/stop', { method: 'POST' });
      refreshUSGS();
    };

    document.getElementById('import-iris').onclick = async () => {
      const res = await fetch('/stations/import/iris?network=IU&limit=12', { method: 'POST' });
      const data = await res.json();
      document.getElementById('iris-message').textContent = `导入 ${data.imported}/${data.total_available} 个台站`;
      refreshStations();
    };

    document.getElementById('refresh-iris').onclick = () => refreshIrisStations();
    document.getElementById('iris-station-select').onchange = () => refreshWaveform();
    document.getElementById('wf-refresh').onclick = () => refreshWaveform();

    document.getElementById('seedlink-start').onclick = async () => {
      await fetch('/iris/live/start', { method: 'POST' });
      await refreshSeedlinkStatus();
    };
    document.getElementById('seedlink-stop').onclick = async () => {
      await fetch('/iris/live/stop', { method: 'POST' });
      await refreshSeedlinkStatus();
    };

    async function boot() {
      await refreshHealth();
      await refreshStations();
      await refreshIrisStations();
      await refreshEvents();
      await refreshWaveform();
      await refreshUSGS();
      await refreshSeedlinkStatus();
      setInterval(() => { refreshHealth(); refreshEvents(); refreshUSGS(); refreshWaveform(); refreshSeedlinkStatus(); refreshLiveFrame(); }, 5000);
    }
    boot();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api", response_class=HTMLResponse)
async def api_root() -> HTMLResponse:
    return HTMLResponse(
        content="""
        <html><body><p>SeismoX realtime catalog API online.</p><p>Docs: <a href='/docs'>/docs</a></p></body></html>
        """
    )
