"""Flask web UI for all three BMW agents.

Endpoints:
  GET  /                   → dashboard + forms for all 3 agents
  POST /api/agent/deal     → stream Agent 1 (SSE: tool_call, tool_result, final)
  POST /api/agent/config   → stream Agent 2
  POST /api/agent/hunter   → stream Agent 3
  GET  /api/traces         → list recent run traces (JSON)
  GET  /api/traces/<name>  → load a specific trace
  GET  /api/db/top         → top deals currently in SQLite
  GET  /api/db/stats       → simple stats for the dashboard
  GET  /screenshots/<name> → serve Agent 2 screenshots

Run with:
    ../venv/bin/python web/app.py
Then open: http://localhost:8000
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

# Make sibling imports work when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import agent as agent_deal  # noqa: E402
import agent_configurator as agent_cfg  # noqa: E402
import agent_hunter as agent_hunt  # noqa: E402
from hunter import database  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/screenshots/<path:filename>")
def screenshots(filename: str):
    return send_from_directory(PROJECT_DIR / "screenshots", filename)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_pack(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _run_agent_sse(runner_thread_target, *args, **kwargs):
    """Run an agent in a background thread that pushes events into a queue.
    Stream events out as SSE until the runner signals completion.
    """
    q: queue.Queue = queue.Queue(maxsize=500)

    def on_event(ev: dict) -> None:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass

    def worker() -> None:
        try:
            final_text, trace_path = runner_thread_target(*args, on_event=on_event, verbose=False, **kwargs)
            q.put({"type": "done", "final": final_text, "trace": os.path.basename(trace_path)})
        except Exception as e:
            q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def stream():
        yield _sse_pack({"type": "connected", "ts": time.time()})
        while True:
            try:
                ev = q.get(timeout=60)
            except queue.Empty:
                yield _sse_pack({"type": "keepalive", "ts": time.time()})
                continue
            yield _sse_pack(ev)
            if ev.get("type") in ("done", "error"):
                break

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@app.route("/api/agent/deal", methods=["POST"])
def api_agent_deal():
    data = request.get_json(silent=True) or {}
    user_input = (data.get("input") or "").strip()
    if not user_input:
        return jsonify({"error": "input is required"}), 400
    return _run_agent_sse(agent_deal.run, user_input)


@app.route("/api/agent/config", methods=["POST"])
def api_agent_config():
    data = request.get_json(silent=True) or {}
    user_input = (data.get("input") or "").strip()
    if not user_input:
        return jsonify({"error": "input is required"}), 400
    return _run_agent_sse(agent_cfg.run, user_input, headed=False)


@app.route("/api/agent/hunter", methods=["POST"])
def api_agent_hunter():
    data = request.get_json(silent=True) or {}
    user_input = (data.get("input") or "").strip()
    real_sources = bool(data.get("real_sources", False))
    return _run_agent_sse(agent_hunt.run, user_input, real_sources=real_sources)


# ---------------------------------------------------------------------------
# Traces + DB
# ---------------------------------------------------------------------------


@app.route("/api/traces")
def api_traces():
    agent = request.args.get("agent")
    limit = int(request.args.get("limit", 30))
    return jsonify(trace_mod.list_traces(agent=agent, limit=limit))


@app.route("/api/traces/<name>")
def api_trace(name: str):
    t = trace_mod.load_trace(name)
    if not t:
        return jsonify({"error": "not found"}), 404
    return jsonify(t)


@app.route("/api/db/top")
def api_db_top():
    limit = int(request.args.get("limit", 10))
    return jsonify(database.get_best_deals(limit))


@app.route("/api/db/stats")
def api_db_stats():
    return jsonify({
        "total_listings": database.count_listings(),
        "has_api_key": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
    })


@app.route("/api/screenshots/recent")
def api_screenshots():
    ss_dir = PROJECT_DIR / "screenshots"
    if not ss_dir.exists():
        return jsonify([])
    files = sorted(ss_dir.glob("*.png"), reverse=True)[:20]
    return jsonify([{"name": f.name, "size": f.stat().st_size} for f in files])


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    print(f"BMW Agents Dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
