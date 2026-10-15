"""Guardian page for one SOS trip.

The car publishes MQTT. This process runs a local broker, subscribes, and
serves the phone page. The page only reads.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import journey

HOST = "127.0.0.1"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def static_file(parts: list[str]) -> Path | None:
    if not parts or parts[0] != "static" or len(parts) < 2:
        return None
    root = STATIC_ROOT.resolve()
    candidate = root.joinpath(*parts[1:]).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


class TripView:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trips: dict[str, dict] = {}

    def update(self, topic: str, payload: dict) -> None:
        parts = topic.split("/")
        if len(parts) < 5:
            return
        token = parts[3]
        kind = parts[4]
        if kind not in {"sos", "track", "state"}:
            return
        with self._lock:
            trip = self._trips.setdefault(token, {})
            trip[kind] = payload

    def snapshot(self, token: str) -> dict:
        with self._lock:
            trip = self._trips.get(token, {})
            return {"sos": trip.get("sos"), "track": trip.get("track"), "state": trip.get("state")}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ride</title>
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<style>
  body { margin: 0; background: #101610; color: #f4f7fb; font-family: sans-serif; }
  header { padding: 18px 16px 8px; }
  h1 { margin: 0; font-size: 32px; }
  .vehicle { color: #c5d0c8; }
  .banner { text-align: center; font-weight: 700; padding: 10px; }
  .banner.ride { background: #1f6b3a; color: #f4fff6; }
  .banner.sos { background: #e10600; color: #fff; }
  .dummy { background: #e0a100; color: #1a1400; text-align: center; font-size: 12px; padding: 4px; }
  #map { height: 52vh; background: #1c2428; }
  .pin-wrap { background: transparent; border: none; }
  .pin { width: 56px; text-align: center; }
  .pin .driver { width: 36px; height: 36px; margin: 0 auto; display: block; object-fit: cover; border-radius: 50%; border: 2px solid #fff; background: #e10600; box-shadow: 0 1px 4px rgba(0,0,0,.5); cursor: pointer; }
  .pin .car { width: 30px; height: 44px; margin: 2px auto 0; filter: drop-shadow(0 2px 2px rgba(0,0,0,.55)); }
  .passenger-head { display: flex; align-items: center; justify-content: space-between; }
  .passenger-head h2 { margin: 0 0 8px; }
  #eye { width: 42px; height: 42px; border: 0; border-radius: 50%; background: #e10600; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; }
  #passenger { display: none; width: 100%; max-height: 140px; object-fit: contain; border-radius: 10px; background: #000; cursor: pointer; }
  #passenger.show { display: block; }
  #passenger-full, #driver-full { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.9); z-index: 3000; align-items: center; justify-content: center; }
  #passenger-full.show, #driver-full.show { display: flex; }
  #passenger-full img, #driver-full img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .leaflet-control-attribution { font-size: 10px; }
  .card { margin: 16px; padding: 12px 14px; background: #161616; border: 1px solid #333; border-radius: 12px; }
  .row { display: flex; justify-content: space-between; padding: 8px 0; border-top: 1px solid #2a2a2a; }
  .critical { color: #ff5a4a; }
  #why { border: 0; border-radius: 999px; background: #243024; color: #f4f7fb; font-weight: 700; padding: 8px 14px; cursor: pointer; }
  #diagnosis h3 { margin: 12px 0 4px; font-size: 16px; }
  #why-description { display: block; text-align: right; max-width: 60%; }
</style>
</head>
<body>
<header>
  <h1 id="title">Ride</h1>
  <div class="vehicle" id="vehicle">CAB-18</div>
</header>
<div class="banner ride" id="banner">Ride live</div>
<div class="dummy">DUMMY DATA</div>
<div id="map"></div>
<section class="card">
  <div class="passenger-head">
    <h2>Passenger</h2>
    <button id="eye" type="button" aria-label="Show passenger">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="currentColor" d="M12 5c5.5 0 9.5 4.2 11 7-1.5 2.8-5.5 7-11 7S2.5 14.8 1 12c1.5-2.8 5.5-7 11-7zm0 2C8 7 4.7 9.8 3.2 12 4.7 14.2 8 17 12 17s7.3-2.8 8.8-5C19.3 9.8 16 7 12 7zm0 2.2A2.8 2.8 0 1 1 12 15a2.8 2.8 0 0 1 0-5.6z"/></svg>
    </button>
  </div>
  <img id="passenger" alt="Passenger">
</section>
<div id="passenger-full"><img id="passenger-large" alt="Passenger"></div>
<div id="driver-full"><img id="driver-large" alt="Driver"></div>
<div class="card" id="detail" hidden>
  <h2>This moment</h2>
  <div class="row"><span>Time</span><span id="d-time">--</span></div>
  <div class="row"><span>Severity</span><span id="d-severity">--</span></div>
  <div class="row"><span>Speed</span><span id="d-speed">--</span></div>
  <div class="row"><span>Front seats</span><span id="d-front">--</span></div>
  <div class="row"><span>Rear seats</span><span id="d-rear">--</span></div>
  <div class="row"><span>Phone</span><span id="d-phone">--</span></div>
  <div class="row"><span>Fix</span><span id="d-fix">--</span></div>
  <img id="d-photo" alt="Cabin still" hidden>
</div>
<section class="card">
  <div class="passenger-head">
    <h2>Safety</h2>
    <button id="why" type="button">Why</button>
  </div>
  <div class="row"><span>Severity</span><span id="severity">--</span></div>
  <div class="row"><span>Front seats</span><span id="front">--</span></div>
  <div class="row"><span>Rear seats</span><span id="rear">--</span></div>
  <div class="row"><span>Phone</span><span id="phone">--</span></div>
  <div class="row"><span>Driver</span><span id="driver">--</span></div>
  <div class="row"><span>Vehicle</span><span id="vehicle-row">--</span></div>
  <div class="row"><span>Speed</span><span id="speed">--</span></div>
  <div class="row"><span>Fix</span><span id="fix">dummy</span></div>
</section>
<section class="card" id="diagnosis" hidden>
  <h2>Diagnosis</h2>
  <h3>Driver</h3>
  <div class="row"><span>Eyes</span><span id="why-eyes">--</span></div>
  <div class="row"><span>Gaze</span><span id="why-gaze">--</span></div>
  <div class="row"><span>Yawn</span><span id="why-yawn">--</span></div>
  <div class="row"><span>Focus</span><span id="why-focus">--</span></div>
  <h3>Passenger</h3>
  <p id="why-empty">No cabin look yet.</p>
  <div id="why-passenger" hidden>
    <div class="row"><span>Event</span><span id="why-event">--</span></div>
    <div class="row"><span>Confidence</span><span id="why-confidence">--</span></div>
    <div class="row"><span>Subjects</span><span id="why-subjects">--</span></div>
    <div class="row"><span>Severity</span><span id="why-severity">--</span></div>
    <div class="row"><span>Description</span><span id="why-description">--</span></div>
  </div>
  <h3>Alert</h3>
  <p id="why-alert">No cabin look yet.</p>
</section>
<script src="/static/leaflet/leaflet.js"></script>
<script>
const token = location.pathname.split("/")[2];
const colors = { green: "#3DDC84", amber: "#E0A100", red: "#FF5A4A" };
const map = L.map("map").setView([28.617, 77.220], 15);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap"
}).addTo(map);
let trail = [];
let pin = null;
let followed = false;
let drawn = 0;
let pinKey = "";
let latest = null;
const passengerFrames = 8;
function passengerSrc(index) {
  const n = ((Math.floor(index / 4) % passengerFrames) + passengerFrames) % passengerFrames;
  return "/static/passenger/" + String(n).padStart(2, "0") + ".jpg";
}
const driverSrc = "/static/driver_face.jpg";
function pinIcon(heading) {
  const turn = Number(heading) || 0;
  const html = '<div class="pin"><img class="driver" src="' + driverSrc + '" alt="Driver">'
    + '<div class="car" style="transform:rotate(' + turn + 'deg)">'
    + '<svg viewBox="0 0 32 48" width="30" height="44" aria-hidden="true">'
    + '<rect x="5" y="2" width="22" height="44" rx="9" fill="#e10600" stroke="#ffffff" stroke-width="2"/>'
    + '<rect x="8" y="8" width="16" height="12" rx="2" fill="#ffe1dc"/>'
    + '<rect x="8" y="26" width="16" height="8" rx="2" fill="#ffc7c2"/>'
    + '</svg></div></div>';
  return L.divIcon({ className: "pin-wrap", html: html, iconSize: [56, 88], iconAnchor: [28, 84] });
}
function revealPassenger() {
  document.getElementById("passenger").classList.add("show");
}
function openDriver() {
  document.getElementById("driver-large").src = driverSrc;
  document.getElementById("driver-full").classList.add("show");
}
document.getElementById("eye").addEventListener("click", function () {
  document.getElementById("passenger").classList.toggle("show");
});
document.getElementById("passenger").addEventListener("click", function () {
  if (!this.src) return;
  document.getElementById("passenger-large").src = this.src;
  document.getElementById("passenger-full").classList.add("show");
});
document.getElementById("passenger-full").addEventListener("click", function () {
  this.classList.remove("show");
});
document.getElementById("driver-full").addEventListener("click", function () {
  this.classList.remove("show");
});
document.getElementById("why").addEventListener("click", function () {
  const card = document.getElementById("diagnosis");
  card.hidden = !card.hidden;
});
function showDiagnosis(state) {
  const driver = state.driver || {};
  document.getElementById("why-eyes").textContent = driver.eyes || "--";
  document.getElementById("why-gaze").textContent = driver.gaze || "--";
  document.getElementById("why-yawn").textContent = driver.yawn || "--";
  document.getElementById("why-focus").textContent = driver.focus || "--";
  const record = state.vlm_record;
  const empty = document.getElementById("why-empty");
  const detail = document.getElementById("why-passenger");
  if (!record) {
    empty.hidden = false;
    detail.hidden = true;
  } else {
    empty.hidden = true;
    detail.hidden = false;
    document.getElementById("why-event").textContent = record.event || "none";
    document.getElementById("why-confidence").textContent = typeof record.confidence === "number" ? record.confidence.toFixed(2) : "--";
    document.getElementById("why-subjects").textContent = (record.subjects || []).join(", ") || "--";
    document.getElementById("why-severity").textContent = record.severity || "--";
    document.getElementById("why-description").textContent = record.description || "--";
  }
  document.getElementById("why-alert").textContent = state.alert_reason || "No cabin look yet.";
}
function showPoint(point) {
  document.getElementById("detail").hidden = false;
  document.getElementById("d-time").textContent = point.ts || "--";
  document.getElementById("d-severity").textContent = point.severity || "--";
  document.getElementById("d-speed").textContent = point.speed_kph != null ? point.speed_kph + " km/h" : "--";
  document.getElementById("d-front").textContent = point.front ?? "--";
  document.getElementById("d-rear").textContent = point.rear ?? "--";
  document.getElementById("d-phone").textContent = point.phone || "--";
  document.getElementById("d-fix").textContent = point.fix || "--";
  const photo = document.getElementById("d-photo");
  if (point.photo_path) {
    photo.hidden = false;
    photo.src = "/t/" + token + "/photo/" + point.id;
  } else {
    photo.hidden = true;
  }
}
function draw(points) {
  if (points.length !== drawn) {
    trail.forEach(function (layer) { map.removeLayer(layer); });
    trail = [];
    points.forEach(function (point, index) {
      if (index === 0) return;
      const prev = points[index - 1];
      const line = L.polyline(
        [[prev.lat, prev.lon], [point.lat, point.lon]],
        { color: colors[point.color] || colors.green, weight: 5, opacity: 0.9 }
      ).addTo(map);
      trail.push(line);
    });
    points.forEach(function (point) {
      const dot = L.circleMarker([point.lat, point.lon], {
        radius: 4,
        color: "#ffffff",
        weight: 1,
        fillColor: colors[point.color] || colors.green,
        fillOpacity: 1
      }).addTo(map);
      dot.on("click", function () { showPoint(point); });
      trail.push(dot);
    });
    drawn = points.length;
  }
  const last = points[points.length - 1];
  latest = last;
  const src = passengerSrc(points.length - 1);
  const key = String(last.heading_deg);
  if (!pin) {
    pin = L.marker([last.lat, last.lon], { icon: pinIcon(last.heading_deg), zIndexOffset: 1000 }).addTo(map);
    pin.on("click", function (event) {
      const target = event.originalEvent && event.originalEvent.target;
      if (target && target.classList && target.classList.contains("driver")) {
        openDriver();
        return;
      }
      revealPassenger();
      if (latest) showPoint(latest);
    });
    pinKey = key;
  } else {
    pin.setLatLng([last.lat, last.lon]);
    if (pinKey !== key) {
      pin.setIcon(pinIcon(last.heading_deg));
      pinKey = key;
    }
  }
  const photo = document.getElementById("passenger");
  if (photo.getAttribute("src") !== src) {
    photo.src = src;
    const large = document.getElementById("passenger-large");
    if (document.getElementById("passenger-full").classList.contains("show")) large.src = src;
  }
  if (!followed) {
    map.setView([last.lat, last.lon], 17);
    followed = true;
  } else {
    map.panTo([last.lat, last.lon], { animate: true, duration: 0.45 });
  }
}
async function tick() {
  const liveResponse = await fetch("/t/" + token + "/live");
  const body = await liveResponse.json();
  const state = body.state || {};
  const track = body.track || {};
  if (state.vehicle_id) document.getElementById("vehicle").textContent = state.vehicle_id;
  const alerted = state.mode === "sos" || state.mode === "incident" || body.sos;
  const banner = document.getElementById("banner");
  const title = document.getElementById("title");
  if (alerted) {
    banner.textContent = "SOS";
    banner.className = "banner sos";
    title.textContent = "SOS";
    document.title = "SOS";
  } else {
    banner.textContent = "Ride live";
    banner.className = "banner ride";
    title.textContent = "Ride";
    document.title = "Ride";
  }
  const severity = document.getElementById("severity");
  severity.textContent = state.severity || "--";
  severity.className = state.severity === "critical" || state.severity === "high" ? "critical" : "";
  document.getElementById("front").textContent = state.front ?? "--";
  document.getElementById("rear").textContent = state.rear ?? "--";
  document.getElementById("phone").textContent = state.phone || "--";
  document.getElementById("driver").textContent = state.driver_id || "--";
  document.getElementById("vehicle-row").textContent = state.vehicle_id || "--";
  document.getElementById("speed").textContent = track.speed_kph != null ? track.speed_kph + " km/h" : "--";
  document.getElementById("fix").textContent = track.fix || "dummy";
  showDiagnosis(state);
  const historyResponse = await fetch("/t/" + token + "/history");
  const points = await historyResponse.json();
  if (Array.isArray(points) && points.length) draw(points);
}
setInterval(tick, 500);
tick();
</script>
</body>
</html>
"""


def start_broker(port: int):
    from amqtt.broker import Broker
    from amqtt.errors import BrokerError

    loop = asyncio.new_event_loop()
    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
        },
        loop=loop,
    )
    try:
        loop.run_until_complete(broker.start())
    except BrokerError as exc:
        if "address already in use" not in str(exc):
            raise
        print(f"[GUARDIAN] mqtt 127.0.0.1:{port} is already open, using that broker")
        return None, None
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return broker, loop


def subscribe(view: TripView, mqtt_port: int) -> None:
    import paho.mqtt.client as mqtt

    announced: set[str] = set()

    def on_message(_client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode())
        except json.JSONDecodeError:
            return
        view.update(message.topic, payload)
        token = str(payload.get("trip_id") or "")
        if token and token not in announced and (message.topic.endswith("/state") or message.topic.endswith("/sos")):
            announced.add(token)
            print(f"[GUARDIAN] open http://127.0.0.1:8080/t/{token}", flush=True)
        if message.topic.endswith("/sos"):
            print(f"[GUARDIAN] SOS on {token}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect(HOST, mqtt_port, keepalive=20)
    client.subscribe("cms/+/trips/#")
    client.loop_start()


class PageServer:
    def __init__(self, http_port: int, view: TripView, store: journey.JourneyStore | None = None) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                return

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                parts = [item for item in path.split("/") if item]
                asset = static_file(parts)
                if asset is not None:
                    body = asset.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", STATIC_TYPES.get(asset.suffix.lower(), "application/octet-stream"))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(parts) == 2 and parts[0] == "t":
                    body = PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(parts) == 3 and parts[0] == "t" and parts[2] == "live":
                    body = json.dumps(owner.view.snapshot(parts[1])).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(parts) == 3 and parts[0] == "t" and parts[2] == "history":
                    body = json.dumps(owner.store.history(parts[1])).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(parts) == 4 and parts[0] == "t" and parts[2] == "photo":
                    point = owner.store.get(parts[1], int(parts[3]))
                    photo = Path(point["photo_path"]) if point and point.get("photo_path") else None
                    root = journey.ROOT / "results"
                    if photo is None or root not in photo.resolve().parents or not photo.is_file():
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = photo.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        self.view = view
        self.store = store or journey.JourneyStore()
        self.http = ThreadingHTTPServer((HOST, http_port), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.http.shutdown()


def serve_in_thread(port: int = 8080, mqtt_port: int = 1883, store: journey.JourneyStore | None = None) -> PageServer:
    start_broker(mqtt_port)
    view = TripView()
    subscribe(view, mqtt_port)
    return PageServer(port, view, store)


def main() -> None:
    serve_in_thread()
    print("[GUARDIAN] waiting for an SOS. The link prints here when the cabin sends one.")
    print("[GUARDIAN] mqtt 127.0.0.1:1883")
    threading.Event().wait()


if __name__ == "__main__":
    main()
