"""
AC Energy Monitor Dashboard
Arduino Nano + ACS712-20A + ZMPT101B + I2C LCD
Tamil Nadu (TNPDCL LT-IA Domestic) tariff estimator
"""

import json
import math
import threading
import time
from collections import deque

import serial
from flask import Flask, Response, jsonify, request

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
SERIAL_PORT = "/dev/cu.usbserial-120"
BAUD_RATE = 9600
HTTP_PORT = 8050          # 5000 is used by AirPlay on macOS
HISTORY_LEN = 300         # points kept for charts
BASE_RATE = 4.95          # Rs/unit, first paid slab

# Tamil Nadu LT-IA domestic, bi-monthly telescopic slabs
# (TNERC Order No. 6 of 2025 + 200 free units scheme from 10 May 2026)
# Each tuple: (upper limit of slab in units, rate Rs/unit)
SLABS_UPTO_500 = [(200, 0.00), (400, 4.95), (500, 6.65)]
SLABS_ABOVE_500 = [
    (100, 0.00), (400, 4.95), (500, 6.65), (600, 8.80),
    (800, 9.95), (1000, 11.05), (math.inf, 12.15),
]

# ------------------------------------------------------------------
# Shared state
# ------------------------------------------------------------------
app = Flask(__name__)
lock = threading.Lock()
ser = None

state = {
    "connected": False,
    "last_update": 0.0,
    "latest": {"v": 0.0, "i": 0.0, "p": 0.0, "e": 0.0, "c": 0.0},
    "history": deque(maxlen=HISTORY_LEN),
    "stats": {},
}


def reset_stats():
    state["stats"] = {
        "start_time": time.time(),
        "samples": 0,
        "sum_p": 0.0,
        "peak_p": 0.0,
        "vmin": math.inf,
        "vmax": 0.0,
    }
    state["history"].clear()


reset_stats()


def tn_bill(units):
    """Bi-monthly TNPDCL domestic bill (energy charges) for given units."""
    slabs = SLABS_UPTO_500 if units <= 500 else SLABS_ABOVE_500
    lower = 0
    total = 0.0
    rows = []
    for upper, rate in slabs:
        if units <= lower:
            break
        slab_units = min(units, upper) - lower
        amount = slab_units * rate
        label = f"{lower + 1}-{int(upper)}" if upper != math.inf else f"Above {lower}"
        rows.append({
            "range": label,
            "units": round(slab_units, 2),
            "rate": rate,
            "amount": round(amount, 2),
        })
        total += amount
        lower = upper
    return round(total, 2), rows


# ------------------------------------------------------------------
# Serial reader thread
# ------------------------------------------------------------------
def serial_reader():
    global ser
    while True:
        try:
            print(f"[serial] Connecting to {SERIAL_PORT} @ {BAUD_RATE}...")
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
            time.sleep(2)                 # Nano auto-resets on connect
            ser.reset_input_buffer()
            with lock:
                state["connected"] = True
                reset_stats()
            print("[serial] Connected.")

            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                now = time.time()
                with lock:
                    state["latest"] = {
                        "v": float(d.get("v", 0)),
                        "i": float(d.get("i", 0)),
                        "p": float(d.get("p", 0)),
                        "e": float(d.get("e", 0)),
                        "c": float(d.get("c", 0)),
                    }
                    state["last_update"] = now
                    L = state["latest"]
                    state["history"].append({"t": now, "v": L["v"], "i": L["i"], "p": L["p"]})

                    s = state["stats"]
                    s["samples"] += 1
                    s["sum_p"] += L["p"]
                    s["peak_p"] = max(s["peak_p"], L["p"])
                    if L["v"] > 0:
                        s["vmin"] = min(s["vmin"], L["v"])
                        s["vmax"] = max(s["vmax"], L["v"])

        except (serial.SerialException, OSError) as e:
            with lock:
                state["connected"] = False
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            print(f"[serial] {e} - retrying in 3 s")
            time.sleep(3)


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
@app.route("/api/data")
def api_data():
    try:
        hours = float(request.args.get("hours", 8))
    except ValueError:
        hours = 8.0
    hours = max(0.0, min(24.0, hours))

    with lock:
        latest = dict(state["latest"])
        history = list(state["history"])
        s = dict(state["stats"])
        connected = state["connected"]
        age = time.time() - state["last_update"] if state["last_update"] else None

    power_kw = latest["p"] / 1000.0
    daily_units = power_kw * hours
    bimonthly_units = daily_units * 60
    bill, rows = tn_bill(bimonthly_units)

    return jsonify({
        "connected": connected,
        "age": age,
        "port": SERIAL_PORT,
        "latest": latest,
        "history": history,
        "stats": {
            "runtime": time.time() - s["start_time"],
            "samples": s["samples"],
            "peak_p": s["peak_p"],
            "avg_p": (s["sum_p"] / s["samples"]) if s["samples"] else 0.0,
            "vmin": None if s["vmin"] == math.inf else s["vmin"],
            "vmax": s["vmax"] if s["vmax"] > 0 else None,
        },
        "projection": {
            "hours": hours,
            "cost_per_hour": round(power_kw * BASE_RATE, 2),
            "daily_units": round(daily_units, 3),
            "bimonthly_units": round(bimonthly_units, 1),
            "bill": bill,
            "monthly": round(bill / 2, 2),
            "effective_rate": round(bill / bimonthly_units, 2) if bimonthly_units > 0 else 0.0,
            "subsidy": "200 free units applied (up to 500 units/cycle)"
                       if bimonthly_units <= 500 else
                       "Only 100 free units (above 500 units/cycle)",
            "subsidy_ok": bimonthly_units <= 500,
            "rows": rows,
        },
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with lock:
        if ser and ser.is_open:
            try:
                ser.write(b"R")
            except Exception:
                pass
        reset_stats()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Frontend
# ------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Energy Monitor - Tamil Nadu</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#0b1020;--panel:#121a2e;--panel2:#18223b;--border:#243150;
  --text:#e6ebf5;--muted:#8a96b3;--accent:#4f8cff;--green:#22c55e;
  --amber:#f59e0b;--red:#ef4444;--violet:#a78bfa;--cyan:#22d3ee;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh}
.wrap{max-width:1400px;margin:0 auto;padding:24px}
header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;margin-bottom:24px}
h1{font-size:22px;font-weight:700;letter-spacing:-.3px}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.actions{display:flex;gap:12px;align-items:center}
.pill{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;
  background:var(--panel);border:1px solid var(--border);font-size:13px;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;background:var(--red)}
.pill.live .dot{background:var(--green);animation:pulse 1.6s infinite}
.pill.wait .dot{background:var(--amber)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.6)}70%{box-shadow:0 0 0 8px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
.btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);padding:8px 14px;
  border-radius:10px;font-size:13px;font-weight:600;cursor:pointer}
.btn:hover{border-color:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px}
.kpi .label{color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;
  letter-spacing:.6px;display:flex;align-items:center;gap:8px}
.kpi .label i{width:8px;height:8px;border-radius:2px;display:inline-block}
.kpi .val{font-size:30px;font-weight:700;margin-top:10px;font-variant-numeric:tabular-nums}
.kpi .unit{font-size:14px;color:var(--muted);font-weight:500;margin-left:4px}
.kpi .hint{font-size:12px;color:var(--muted);margin-top:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.grid3{display:grid;grid-template-columns:2fr 1fr;gap:16px}
@media(max-width:1000px){.grid2,.grid3{grid-template-columns:1fr}}
.card h2{font-size:15px;font-weight:600;margin-bottom:4px}
.card .desc{font-size:12px;color:var(--muted);margin-bottom:14px}
.chartbox{position:relative;height:260px}
.slider{display:flex;align-items:center;gap:14px;margin:6px 0 18px}
input[type=range]{flex:1;accent-color:var(--accent)}
.badge{background:var(--panel2);border:1px solid var(--border);padding:4px 10px;border-radius:8px;
  font-size:13px;font-weight:600;min-width:90px;text-align:center}
.proj{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.proj div{background:var(--panel2);border-radius:12px;padding:12px}
.proj span{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.proj b{display:block;font-size:20px;margin-top:6px;font-variant-numeric:tabular-nums}
.proj .big b{color:var(--green)}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tfoot td{font-weight:700;border-bottom:none}
.tag{display:inline-block;margin-bottom:10px;padding:4px 10px;border-radius:8px;font-size:12px;
  font-weight:600;background:rgba(34,197,94,.12);color:var(--green)}
.tag.warn{background:rgba(245,158,11,.12);color:var(--amber)}
.note{font-size:12px;color:var(--muted);margin-top:12px;line-height:1.5}
.row{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--border);font-size:14px}
.row:last-child{border-bottom:none}
.row span{color:var(--muted)}
.row b{font-variant-numeric:tabular-nums}
footer{text-align:center;color:var(--muted);font-size:12px;margin-top:24px}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>AC Energy Monitor</h1>
      <div class="sub">Arduino Nano &middot; ACS712-20A &middot; ZMPT101B &middot; <span id="port">-</span></div>
    </div>
    <div class="actions">
      <div class="pill" id="status"><span class="dot"></span><span id="statusText">Connecting...</span></div>
      <button class="btn" id="resetBtn">Reset session</button>
    </div>
  </header>

  <section class="kpis">
    <div class="card kpi">
      <div class="label"><i style="background:var(--accent)"></i>Voltage</div>
      <div class="val"><span id="v">0.0</span><span class="unit">V</span></div>
      <div class="hint">RMS mains voltage</div>
    </div>
    <div class="card kpi">
      <div class="label"><i style="background:var(--cyan)"></i>Current</div>
      <div class="val"><span id="i">0.000</span><span class="unit">A</span></div>
      <div class="hint" id="loadState">No load</div>
    </div>
    <div class="card kpi">
      <div class="label"><i style="background:var(--amber)"></i>Power</div>
      <div class="val"><span id="p">0.0</span><span class="unit">VA</span></div>
      <div class="hint">Apparent power (V x I)</div>
    </div>
    <div class="card kpi">
      <div class="label"><i style="background:var(--violet)"></i>Energy</div>
      <div class="val"><span id="e">0.0000</span><span class="unit">kWh</span></div>
      <div class="hint">Units used this session</div>
    </div>
    <div class="card kpi">
      <div class="label"><i style="background:var(--green)"></i>Session Cost</div>
      <div class="val"><span id="c">&#8377;0.00</span></div>
      <div class="hint">At &#8377;4.95 / unit base rate</div>
    </div>
    <div class="card kpi">
      <div class="label"><i style="background:var(--muted)"></i>Runtime</div>
      <div class="val"><span id="rt">00:00:00</span></div>
      <div class="hint" id="samples">0 samples</div>
    </div>
  </section>

  <section class="grid2">
    <div class="card">
      <h2>Voltage &amp; Current</h2>
      <div class="desc">Live RMS readings</div>
      <div class="chartbox"><canvas id="viChart"></canvas></div>
    </div>
    <div class="card">
      <h2>Power</h2>
      <div class="desc">Apparent power drawn by the load</div>
      <div class="chartbox"><canvas id="pChart"></canvas></div>
    </div>
  </section>

  <section class="grid3">
    <div class="card">
      <h2>Tamil Nadu Bill Estimator (TNPDCL LT-IA Domestic)</h2>
      <div class="desc">Projected bi-monthly bill if the current load runs daily for the hours below</div>

      <div class="slider">
        <input type="range" id="hours" min="1" max="24" step="1" value="8">
        <div class="badge" id="hoursVal">8 h / day</div>
      </div>

      <div class="proj">
        <div><span>Cost per hour</span><b id="cph">&#8377;0.00</b></div>
        <div><span>Units per day</span><b id="du">0.00</b></div>
        <div><span>Units per 2 months</span><b id="bu">0</b></div>
        <div class="big"><span>Bi-monthly bill</span><b id="bill">&#8377;0.00</b></div>
        <div><span>Per month (approx)</span><b id="mon">&#8377;0.00</b></div>
        <div><span>Effective rate</span><b id="eff">&#8377;0.00</b></div>
      </div>

      <div class="tag" id="subsidy">-</div>

      <table>
        <thead><tr><th>Slab (units)</th><th>Units</th><th>Rate (&#8377;/unit)</th><th>Amount</th></tr></thead>
        <tbody id="slabRows"></tbody>
        <tfoot><tr><td>Total energy charges</td><td></td><td></td><td id="slabTotal">&#8377;0.00</td></tr></tfoot>
      </table>

      <div class="note">
        Slabs per TNERC Order No. 6 of 2025 with the 200 free units scheme (from 10 May 2026).
        Bi-monthly, telescopic billing. Estimate assumes only this appliance is on the meter;
        your real bill depends on total household usage.
      </div>
    </div>

    <div class="card">
      <h2>Session Statistics</h2>
      <div class="desc">Since the dashboard connected or was reset</div>
      <div class="row"><span>Peak power</span><b id="peak">0.0 VA</b></div>
      <div class="row"><span>Average power</span><b id="avg">0.0 VA</b></div>
      <div class="row"><span>Min voltage</span><b id="vmin">-</b></div>
      <div class="row"><span>Max voltage</span><b id="vmax">-</b></div>
      <div class="row"><span>Energy used</span><b id="e2">0.0000 kWh</b></div>
      <div class="row"><span>Session cost</span><b id="c2">&#8377;0.00</b></div>
    </div>
  </section>

  <footer>Readings accurate to roughly &plusmn;5&ndash;10% &middot; Updates every second</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const inr = n => '\\u20B9' + Number(n).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
let hours = 8;

$('hours').addEventListener('input', e => {
  hours = Number(e.target.value);
  $('hoursVal').textContent = hours + ' h / day';
  tick();
});

$('resetBtn').addEventListener('click', async () => {
  await fetch('/api/reset', {method: 'POST'});
  tick();
});

function fmtDur(s) {
  s = Math.floor(s || 0);
  const h = String(Math.floor(s / 3600)).padStart(2, '0');
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const sec = String(s % 60).padStart(2, '0');
  return h + ':' + m + ':' + sec;
}

const gridColor = 'rgba(138,150,179,0.12)';
const tickColor = '#8a96b3';

function baseOpts() {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: {mode: 'index', intersect: false},
    plugins: {legend: {labels: {color: tickColor, boxWidth: 12}}},
    scales: {x: {ticks: {color: tickColor, maxTicksLimit: 8}, grid: {color: gridColor}}}
  };
}

const viOpts = baseOpts();
viOpts.scales.y = {position: 'left', ticks: {color: '#4f8cff'}, grid: {color: gridColor},
  title: {display: true, text: 'Volts', color: tickColor}};
viOpts.scales.y1 = {position: 'right', beginAtZero: true, ticks: {color: '#22d3ee'},
  grid: {drawOnChartArea: false}, title: {display: true, text: 'Amps', color: tickColor}};

const viChart = new Chart($('viChart'), {
  type: 'line',
  data: {labels: [], datasets: [
    {label: 'Voltage (V)', data: [], borderColor: '#4f8cff', borderWidth: 2, pointRadius: 0, tension: 0.3, yAxisID: 'y'},
    {label: 'Current (A)', data: [], borderColor: '#22d3ee', borderWidth: 2, pointRadius: 0, tension: 0.3, yAxisID: 'y1'}
  ]},
  options: viOpts
});

const pOpts = baseOpts();
pOpts.scales.y = {beginAtZero: true, ticks: {color: tickColor}, grid: {color: gridColor},
  title: {display: true, text: 'VA', color: tickColor}};

const pChart = new Chart($('pChart'), {
  type: 'line',
  data: {labels: [], datasets: [
    {label: 'Power (VA)', data: [], borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.15)',
     fill: true, borderWidth: 2, pointRadius: 0, tension: 0.3}
  ]},
  options: pOpts
});

function setStatus(cls, text) {
  $('status').className = 'pill ' + cls;
  $('statusText').textContent = text;
}

function render(d) {
  $('port').textContent = d.port;

  if (d.connected && d.age !== null && d.age < 5) setStatus('live', 'Live');
  else if (d.connected) setStatus('wait', 'Waiting for data');
  else setStatus('', 'Disconnected');

  const L = d.latest;
  $('v').textContent = L.v.toFixed(1);
  $('i').textContent = L.i.toFixed(3);
  $('p').textContent = L.p.toFixed(1);
  $('e').textContent = L.e.toFixed(4);
  $('c').textContent = inr(L.c);
  $('loadState').textContent = L.i > 0 ? 'Load connected' : 'No load';

  const S = d.stats;
  $('rt').textContent = fmtDur(S.runtime);
  $('samples').textContent = S.samples + ' samples';
  $('peak').textContent = S.peak_p.toFixed(1) + ' VA';
  $('avg').textContent = S.avg_p.toFixed(1) + ' VA';
  $('vmin').textContent = S.vmin !== null ? S.vmin.toFixed(1) + ' V' : '-';
  $('vmax').textContent = S.vmax !== null ? S.vmax.toFixed(1) + ' V' : '-';
  $('e2').textContent = L.e.toFixed(4) + ' kWh';
  $('c2').textContent = inr(L.c);

  const P = d.projection;
  $('cph').textContent = inr(P.cost_per_hour);
  $('du').textContent = P.daily_units.toFixed(2);
  $('bu').textContent = P.bimonthly_units.toFixed(1);
  $('bill').textContent = inr(P.bill);
  $('mon').textContent = inr(P.monthly);
  $('eff').textContent = inr(P.effective_rate) + '/u';
  $('subsidy').textContent = P.subsidy;
  $('subsidy').className = 'tag' + (P.subsidy_ok ? '' : ' warn');

  $('slabRows').innerHTML = P.rows.length
    ? P.rows.map(r => `<tr><td>${r.range}</td><td>${r.units.toFixed(1)}</td><td>${r.rate.toFixed(2)}</td><td>${inr(r.amount)}</td></tr>`).join('')
    : '<tr><td colspan="4" style="text-align:center;color:#8a96b3">No load - connect an appliance</td></tr>';
  $('slabTotal').textContent = inr(P.bill);

  const labels = d.history.map(h => new Date(h.t * 1000).toLocaleTimeString('en-IN', {hour12: false}));
  viChart.data.labels = labels;
  viChart.data.datasets[0].data = d.history.map(h => h.v);
  viChart.data.datasets[1].data = d.history.map(h => h.i);
  viChart.update();
  pChart.data.labels = labels;
  pChart.data.datasets[0].data = d.history.map(h => h.p);
  pChart.update();
}

async function tick() {
  try {
    const r = await fetch('/api/data?hours=' + hours);
    render(await r.json());
  } catch (e) {
    setStatus('', 'Server offline');
  }
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


if __name__ == "__main__":
    threading.Thread(target=serial_reader, daemon=True).start()
    print(f"Dashboard: http://localhost:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False, use_reloader=False)