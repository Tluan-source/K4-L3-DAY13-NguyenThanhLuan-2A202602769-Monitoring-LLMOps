"""Runtime dashboard: 6 panels from data/logs.jsonl (config/dashboard.yaml)."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio

LOG_PATH = REPO_ROOT / "data" / "logs.jsonl"
TIME_RANGE_MINUTES = 60
REFRESH_SECONDS = 30
HOST = "127.0.0.1"
PORT = 8501

THRESHOLDS = {
    "latency_p95": 3000,
    "traffic_rpm": 1,
    "error_rate_pct": 2,
    "cost_total": 2.5,
    "tokens_total": 50000,
    "quality_mean": 0.75,
    "retrieval_success_pct": 90,
}


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _minute_bucket(ts: datetime) -> str:
    return ts.replace(second=0, microsecond=0).strftime("%H:%M")


def load_records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    records: list[dict] = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rec["_ts"] = _parse_ts(rec.get("ts"))
            records.append(rec)
    return records


def window_records(records: list[dict]) -> tuple[list[dict], str]:
    dated = [r for r in records if r.get("_ts") is not None]
    if not dated:
        return [], "no-timestamp"
    latest = max(r["_ts"] for r in dated)
    start = latest - timedelta(minutes=TIME_RANGE_MINUTES)
    windowed = [r for r in dated if r["_ts"] >= start]
    if windowed:
        return windowed, "last_60_min_of_log"
    return dated, "all_logs_fallback"


def compute(records: list[dict]) -> dict:
    received = [r for r in records if r.get("event") == "request_received"]
    sent = [r for r in records if r.get("event") == "response_sent"]
    failed = [r for r in records if r.get("event") == "request_failed"]

    latencies = [float(r["latency_ms"]) for r in sent if isinstance(r.get("latency_ms"), (int, float))]
    ttfts = [float(r["ttft_ms"]) for r in sent if isinstance(r.get("ttft_ms"), (int, float))]
    costs = [float(r["cost_usd"]) for r in sent if isinstance(r.get("cost_usd"), (int, float))]
    tokens_in = [float(r["tokens_in"]) for r in sent if isinstance(r.get("tokens_in"), (int, float))]
    tokens_out = [float(r["tokens_out"]) for r in sent if isinstance(r.get("tokens_out"), (int, float))]
    quality = [float(r["quality_score"]) for r in sent if isinstance(r.get("quality_score"), (int, float))]

    tool_flags = [bool(r.get("tool_success")) for r in records if r.get("tool_success") is not None]
    retrieval_success = (
        (sum(1 for flag in tool_flags if flag) / len(tool_flags) * 100.0) if tool_flags else None
    )
    error_rate = (len(failed) / len(received) * 100.0) if received else None
    error_types = Counter(str(r.get("error_type") or "unknown") for r in failed)

    rpm: dict[str, int] = defaultdict(int)
    cost_by_min: dict[str, float] = defaultdict(float)
    lat_by_min: dict[str, list[float]] = defaultdict(list)
    for rec in received:
        rpm[_minute_bucket(rec["_ts"])] += 1
    for rec in sent:
        bucket = _minute_bucket(rec["_ts"])
        if isinstance(rec.get("cost_usd"), (int, float)):
            cost_by_min[bucket] += float(rec["cost_usd"])
        if isinstance(rec.get("latency_ms"), (int, float)):
            lat_by_min[bucket].append(float(rec["latency_ms"]))

    minutes = sorted(set(rpm) | set(cost_by_min) | set(lat_by_min))
    p95_series = [_percentile(lat_by_min.get(m, []), 95) for m in minutes]

    def _round(value: float | None, digits: int = 2) -> float | None:
        if value is None:
            return None
        return round(value, digits)

    return {
        "received": len(received),
        "sent": len(sent),
        "failed": len(failed),
        "latency_p50": _round(_percentile(latencies, 50), 1),
        "latency_p95": _round(_percentile(latencies, 95), 1),
        "latency_p99": _round(_percentile(latencies, 99), 1),
        "ttft_p95": _round(_percentile(ttfts, 95), 1),
        "error_rate_pct": _round(error_rate, 2),
        "retrieval_success_pct": _round(retrieval_success, 1),
        "error_types": dict(error_types),
        "cost_total": _round(sum(costs), 6),
        "tokens_in": int(sum(tokens_in)),
        "tokens_out": int(sum(tokens_out)),
        "quality_mean": _round(sum(quality) / len(quality) if quality else None, 3),
        "minutes": minutes,
        "rpm": [rpm.get(m, 0) for m in minutes],
        "cost_by_min": [_round(cost_by_min.get(m, 0.0), 6) for m in minutes],
        "p95_by_min": [_round(v, 1) if v is not None else None for v in p95_series],
        "avg_rpm": _round((len(received) / TIME_RANGE_MINUTES) if received else 0.0, 2),
    }


def _fmt(value: object, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def _status(value: float | None, operator: str, limit: float) -> tuple[str, str]:
    if value is None:
        return "NO DATA", "muted"
    ok = value <= limit if operator == "lte" else value >= limit
    return ("OK", "ok") if ok else ("BREACH", "breach")


def render_html(records: list[dict], mode: str) -> str:
    stats = compute(records)
    payload = json.dumps(stats, ensure_ascii=False)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    range_label = (
        "Last 60 minutes of log timestamps"
        if mode == "last_60_min_of_log"
        else "All logs (no events in the last 60 minutes of wall-clock; using log span)"
    )

    lat_s, lat_c = _status(stats["latency_p95"], "lte", THRESHOLDS["latency_p95"])
    traf_s, traf_c = _status(stats["avg_rpm"], "gte", THRESHOLDS["traffic_rpm"])
    err_s, err_c = _status(stats["error_rate_pct"] if stats["error_rate_pct"] is not None else 0.0, "lte", THRESHOLDS["error_rate_pct"])
    cost_s, cost_c = _status(stats["cost_total"] if stats["cost_total"] is not None else 0.0, "lte", THRESHOLDS["cost_total"])
    tok_s, tok_c = _status(float(stats["tokens_in"] + stats["tokens_out"]), "lte", THRESHOLDS["tokens_total"])
    qual_s, qual_c = _status(stats["quality_mean"], "gte", THRESHOLDS["quality_mean"])
    ret_s, ret_c = _status(stats["retrieval_success_pct"], "gte", THRESHOLDS["retrieval_success_pct"])

    errors_html = "".join(
        f"<li>{name}: {count}</li>" for name, count in stats["error_types"].items()
    ) or "<li>none</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="{REFRESH_SECONDS}" />
  <title>K4-L3B Day 13 Monitoring & LLMOps</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: Segoe UI, sans-serif; margin: 0; background: #0f1419; color: #e7ecf3; }}
    header {{ padding: 16px 24px; background: #161d27; border-bottom: 1px solid #2a3545; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    .meta {{ color: #9aa8b8; font-size: 13px; }}
    main {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px 24px 32px; }}
    .panel {{ background: #161d27; border: 1px solid #2a3545; border-radius: 10px; padding: 14px 16px; }}
    .panel h2 {{ margin: 0 0 8px; font-size: 16px; }}
    .kpis {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }}
    .kpi {{ background: #0f1419; border-radius: 8px; padding: 8px 10px; min-width: 110px; }}
    .kpi .label {{ font-size: 11px; color: #9aa8b8; }}
    .kpi .value {{ font-size: 20px; font-weight: 700; }}
    .ok {{ color: #3dd68c; }} .breach {{ color: #ff6b6b; }} .muted {{ color: #9aa8b8; }}
    canvas {{ max-height: 180px; }}
    ul {{ margin: 6px 0 0; padding-left: 18px; color: #c5d0dc; font-size: 13px; }}
  </style>
</head>
<body>
  <header>
    <h1>K4-L3B Day 13 Monitoring &amp; LLMOps</h1>
    <div class="meta">
      Source: data/logs.jsonl · Time range: 60 minutes · Refresh: {REFRESH_SECONDS}s · Rendered: {now}<br/>
      Window: {range_label} · events in window: {len(records)}
    </div>
  </header>
  <main>
    <section class="panel" id="latency">
      <h2>Latency percentiles and TTFT <span class="{lat_c}">[{lat_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">P50 (ms)</div><div class="value">{_fmt(stats['latency_p50'])}</div></div>
        <div class="kpi"><div class="label">P95 (ms) ≤ 3000</div><div class="value {lat_c}">{_fmt(stats['latency_p95'])}</div></div>
        <div class="kpi"><div class="label">P99 (ms)</div><div class="value">{_fmt(stats['latency_p99'])}</div></div>
        <div class="kpi"><div class="label">TTFT P95 (ms)</div><div class="value">{_fmt(stats['ttft_p95'])}</div></div>
      </div>
      <canvas id="chartLatency"></canvas>
    </section>
    <section class="panel" id="traffic">
      <h2>Request traffic <span class="{traf_c}">[{traf_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">request_received</div><div class="value">{stats['received']}</div></div>
        <div class="kpi"><div class="label">avg / min ≥ 1</div><div class="value {traf_c}">{_fmt(stats['avg_rpm'])}</div></div>
      </div>
      <canvas id="chartTraffic"></canvas>
    </section>
    <section class="panel" id="errors">
      <h2>Error rate and retrieval success <span class="{err_c}">[{err_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">error rate % ≤ 2</div><div class="value {err_c}">{_fmt(stats['error_rate_pct'], '%')}</div></div>
        <div class="kpi"><div class="label">failed</div><div class="value">{stats['failed']}</div></div>
        <div class="kpi"><div class="label">retrieval success ≥ 90%</div><div class="value {ret_c}">{_fmt(stats['retrieval_success_pct'], '%')}</div></div>
      </div>
      <div class="meta">error_type breakdown</div>
      <ul>{errors_html}</ul>
    </section>
    <section class="panel" id="cost">
      <h2>Cost over time <span class="{cost_c}">[{cost_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">total USD ≤ 2.5</div><div class="value {cost_c}">{_fmt(stats['cost_total'])}</div></div>
      </div>
      <canvas id="chartCost"></canvas>
    </section>
    <section class="panel" id="tokens">
      <h2>Input and output tokens <span class="{tok_c}">[{tok_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">tokens_in</div><div class="value">{stats['tokens_in']}</div></div>
        <div class="kpi"><div class="label">tokens_out</div><div class="value">{stats['tokens_out']}</div></div>
        <div class="kpi"><div class="label">sum ≤ 50000</div><div class="value {tok_c}">{stats['tokens_in'] + stats['tokens_out']}</div></div>
      </div>
      <canvas id="chartTokens"></canvas>
    </section>
    <section class="panel" id="quality">
      <h2>Quality proxy <span class="{qual_c}">[{qual_s}]</span></h2>
      <div class="kpis">
        <div class="kpi"><div class="label">mean score ≥ 0.75</div><div class="value {qual_c}">{_fmt(stats['quality_mean'])}</div></div>
        <div class="kpi"><div class="label">unit</div><div class="value">0–1</div></div>
      </div>
      <canvas id="chartQuality"></canvas>
    </section>
  </main>
  <script>
    const data = {payload};
    const labels = data.minutes;
    const threshPlugin = (yValue, color) => ({{
      id: 'slo-' + yValue,
      afterDraw(chart) {{
        const y = chart.scales.y.getPixelForValue(yValue);
        const {{left, right}} = chart.chartArea;
        const ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.restore();
      }}
    }});
    const common = {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '#c5d0dc' }} }} }} }};
    new Chart(document.getElementById('chartLatency'), {{
      type: 'line',
      data: {{ labels, datasets: [{{ label: 'P95 latency_ms / min', data: data.p95_by_min, borderColor: '#7aa2ff', tension: 0.2 }}] }},
      options: {{ ...common, scales: {{ y: {{ beginAtZero: true, suggestedMax: 3200, title: {{ display: true, text: 'ms' }} }} }} }},
      plugins: [threshPlugin(3000, '#ff6b6b')]
    }});
    new Chart(document.getElementById('chartTraffic'), {{
      type: 'bar',
      data: {{ labels, datasets: [{{ label: 'request_received / min', data: data.rpm, backgroundColor: '#3dd68c' }}] }},
      options: {{ ...common, scales: {{ y: {{ title: {{ display: true, text: 'requests_per_minute' }}, beginAtZero: true }} }} }}
    }});
    new Chart(document.getElementById('chartCost'), {{
      type: 'line',
      data: {{ labels, datasets: [{{ label: 'cost_usd / min', data: data.cost_by_min, borderColor: '#f5c542', tension: 0.2 }}] }},
      options: {{ ...common, scales: {{ y: {{ title: {{ display: true, text: 'usd' }} }} }} }}
    }});
    new Chart(document.getElementById('chartTokens'), {{
      type: 'bar',
      data: {{ labels: ['tokens_in', 'tokens_out'], datasets: [{{ label: 'sum', data: [data.tokens_in, data.tokens_out], backgroundColor: ['#7aa2ff', '#c084fc'] }}] }},
      options: {{ ...common, scales: {{ y: {{ title: {{ display: true, text: 'tokens' }}, beginAtZero: true }} }} }}
    }});
    new Chart(document.getElementById('chartQuality'), {{
      type: 'bar',
      data: {{ labels: ['mean quality_score'], datasets: [{{ label: 'score_0_to_1', data: [data.quality_mean], backgroundColor: '#3dd68c' }}] }},
      options: {{ ...common, scales: {{ y: {{ min: 0, max: 1, title: {{ display: true, text: 'score_0_to_1' }} }} }} }},
      plugins: [threshPlugin(0.75, '#f5c542')]
    }});
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        records, mode = window_records(load_records())
        body = render_html(records, mode).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> int:
    configure_utf8_stdio()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Dashboard: http://{HOST}:{PORT}")
    print(f"Source: {LOG_PATH} · time range {TIME_RANGE_MINUTES}m · refresh {REFRESH_SECONDS}s")
    print("Chụp 6 panel (tên, đơn vị, time range, đường threshold). Ctrl+C để dừng.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
