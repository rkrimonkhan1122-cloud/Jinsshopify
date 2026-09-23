"""
Website Live/Dead Checker - API
-------------------------------
A Flask web service that exposes the same website-checking logic as the
original Tkinter GUI. Designed to be deployed to Railway (or any container
host) directly from a GitHub repo.

Endpoints
~~~~~~~~~
GET  /            -> HTML UI (mirrors the desktop GUI: textarea + results table)
GET  /health      -> {"status":"ok"} (Railway health check)
POST /api/check    -> JSON:  {"urls": ["..."], "timeout": 10, "threads": 8}
                   -> JSON:  {"results": [...], "summary": {...}}
GET  /api/check   -> ?url=...&url=...&timeout=10&threads=8  (same response)

Author: Super Z
"""

from __future__ import annotations

import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Iterable

import requests
from flask import Flask, Response, jsonify, render_template_string, request
from requests.exceptions import RequestException

# --------------------------------------------------------------------------- #
# Configuration (Railway sets PORT via env)
# --------------------------------------------------------------------------- #
DEFAULT_TIMEOUT = 10      # seconds per request
DEFAULT_THREADS = 8       # concurrent workers
MAX_THREADS = 32
MAX_URLS_PER_REQUEST = 200


# --------------------------------------------------------------------------- #
# Core checking logic (kept identical to the desktop GUI version)
# --------------------------------------------------------------------------- #
def check_website(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    Check whether a single website URL is reachable.

    Returns a dict with keys:
        url, status, code, response_time, final_url, error
    """
    result: dict = {
        "url": url,
        "status": "UNKNOWN",
        "code": "",
        "response_time": "",
        "final_url": "",
        "error": "",
    }

    if not url or not str(url).strip():
        result["error"] = "Empty URL"
        result["status"] = "DEAD"
        return result

    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    start = time.perf_counter()
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            verify=True,
        )
        elapsed = (time.perf_counter() - start) * 1000  # ms

        result["code"] = str(resp.status_code)
        result["response_time"] = f"{elapsed:.0f} ms"
        result["final_url"] = resp.url

        if 200 <= resp.status_code < 400:
            result["status"] = "LIVE"
        elif 400 <= resp.status_code < 500:
            result["status"] = "WARNING"
            result["error"] = f"Client error ({resp.status_code})"
        else:
            result["status"] = "DEAD"
            result["error"] = f"Server error ({resp.status_code})"

    except requests.exceptions.ConnectTimeout:
        result["status"] = "DEAD"
        result["error"] = "Connection timed out"
    except requests.exceptions.ReadTimeout:
        result["status"] = "DEAD"
        result["error"] = "Read timed out"
    except requests.exceptions.ConnectionError:
        result["status"] = "DEAD"
        result["error"] = "Connection error"
    except requests.exceptions.SSLError:
        result["status"] = "WARNING"
        result["error"] = "SSL error"
    except requests.exceptions.InvalidURL:
        result["status"] = "DEAD"
        result["error"] = "Invalid URL"
    except RequestException as e:
        result["status"] = "DEAD"
        result["error"] = f"{e.__class__.__name__}"
    except Exception as e:  # noqa: BLE001 - last-resort safety net
        result["status"] = "DEAD"
        result["error"] = f"Unexpected: {e.__class__.__name__}"

    return result


def check_many(
    urls: Iterable[str],
    timeout: int = DEFAULT_TIMEOUT,
    threads: int = DEFAULT_THREADS,
) -> list[dict]:
    """Run check_website across many URLs concurrently, preserving input order."""
    urls_list = list(urls)
    if not urls_list:
        return []

    threads = max(1, min(threads, MAX_THREADS))
    results: list[dict | None] = [None] * len(urls_list)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        future_to_idx = {
            pool.submit(check_website, url, timeout): idx
            for idx, url in enumerate(urls_list)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:  # noqa: BLE001
                results[idx] = {
                    "url": urls_list[idx],
                    "status": "DEAD",
                    "code": "",
                    "response_time": "",
                    "final_url": "",
                    "error": f"Unexpected: {e.__class__.__name__}",
                }

    return [r for r in results if r is not None]


def summarize(results: list[dict]) -> dict:
    return {
        "total": len(results),
        "live": sum(1 for r in results if r["status"] == "LIVE"),
        "warning": sum(1 for r in results if r["status"] == "WARNING"),
        "dead": sum(1 for r in results if r["status"] == "DEAD"),
    }


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
app = Flask(__name__)


@app.route("/")
def index() -> str:
    """Serve a minimal HTML page that mirrors the desktop GUI."""
    return render_template_string(INDEX_HTML)


@app.route("/health")
def health() -> tuple[Response, int]:
    return jsonify({"status": "ok"}), 200


def _parse_params() -> tuple[list[str], int, int, str | None]:
    """Extract urls, timeout, threads from either JSON body or query string."""
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        if isinstance(data, dict):
            urls = data.get("urls", [])
            if isinstance(urls, str):
                urls = [u for u in urls.splitlines() if u.strip()]
            timeout = int(data.get("timeout", DEFAULT_TIMEOUT))
            threads = int(data.get("threads", DEFAULT_THREADS))
        else:
            urls, timeout, threads = [], DEFAULT_TIMEOUT, DEFAULT_THREADS
    else:
        urls = request.args.getlist("url")
        if not urls:
            raw = request.args.get("urls", "")
            urls = [u for u in raw.splitlines() if u.strip()]
        timeout = int(request.args.get("timeout", DEFAULT_TIMEOUT))
        threads = int(request.args.get("threads", DEFAULT_THREADS))

    return urls, timeout, threads, None


@app.route("/api/check", methods=["GET", "POST"])
def api_check():
    try:
        urls, timeout, threads, _ = _parse_params()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid timeout or threads value"}), 400

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    if len(urls) > MAX_URLS_PER_REQUEST:
        return jsonify(
            {"error": f"Too many URLs (max {MAX_URLS_PER_REQUEST} per request)"}
        ), 400

    timeout = max(1, min(timeout, 60))
    threads = max(1, min(threads, MAX_THREADS))

    results = check_many(urls, timeout=timeout, threads=threads)
    payload = {
        "results": results,
        "summary": summarize(results),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    return jsonify(payload), 200


@app.route("/api/check/csv", methods=["GET", "POST"])
def api_check_csv():
    """Same as /api/check but returns a CSV download."""
    try:
        urls, timeout, threads, _ = _parse_params()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid timeout or threads value"}), 400

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    timeout = max(1, min(timeout, 60))
    threads = max(1, min(threads, MAX_THREADS))
    results = check_many(urls, timeout=timeout, threads=threads)

    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=["url", "status", "code", "response_time", "final_url", "error"],
    )
    writer.writeheader()
    writer.writerows(results)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=website_check_{timestamp}.csv"
        },
    )


# --------------------------------------------------------------------------- #
# HTML UI (mirrors the desktop GUI look & feel)
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Website Live / Dead Checker</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #f5f6f8; color: #111827; margin: 0; padding: 24px;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .subtitle { color: #6b7280; font-size: 13px; margin-bottom: 16px; }
  .card {
    background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
    padding: 16px; margin-bottom: 16px;
  }
  textarea {
    width: 100%; font-family: Consolas, monospace; font-size: 13px;
    padding: 10px; border: 1px solid #e5e7eb; border-radius: 6px;
    resize: vertical; min-height: 120px;
  }
  textarea:focus { outline: none; border-color: #2563eb; }
  .options { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
             margin-top: 12px; }
  label { font-size: 13px; color: #374151; }
  input[type=number] { width: 70px; padding: 6px; border: 1px solid #e5e7eb;
                       border-radius: 4px; font-size: 13px; }
  button {
    background: #2563eb; color: #fff; border: none; border-radius: 6px;
    padding: 9px 18px; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #9ca3af; cursor: not-allowed; }
  button.secondary { background: #e5e7eb; color: #374151; }
  button.secondary:hover { background: #d1d5db; }
  .status-bar {
    font-size: 13px; color: #6b7280; margin-top: 8px;
  }
  table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f3f4f6; }
  th { background: #f9fafb; font-weight: 600; color: #374151; }
  tr.LIVE { background: #dcfce7; color: #166534; }
  tr.DEAD { background: #fee2e2; color: #991b1b; }
  tr.WARNING { background: #fef9c3; color: #854d0e; }
  tr.UNKNOWN { background: #f3f4f6; color: #374151; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-weight: 600; font-size: 11px; letter-spacing: 0.3px;
  }
  .pill.LIVE { background: #16a34a; color: #fff; }
  .pill.DEAD { background: #dc2626; color: #fff; }
  .pill.WARNING { background: #ca8a04; color: #fff; }
  .pill.UNKNOWN { background: #6b7280; color: #fff; }
  .summary { display: flex; gap: 16px; font-size: 13px; margin-top: 12px;
             color: #374151; }
  .summary b { font-weight: 600; }
  .error-msg { color: #dc2626; font-size: 13px; margin-top: 8px; }
  code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px;
         font-size: 12px; }
</style>
</head>
<body>
<div class="container">
  <h1>Website Live / Dead Checker</h1>
  <p class="subtitle">
    Enter one URL per line, then click Check. Same logic as the desktop GUI,
    now served from Railway.
  </p>

  <div class="card">
    <textarea id="urls" placeholder="https://example.com&#10;https://github.com&#10;https://httpbin.org/status/500">https://example.com
https://github.com
https://httpbin.org/status/500</textarea>
    <div class="options">
      <label>Timeout (s):</label>
      <input type="number" id="timeout" value="10" min="1" max="60">
      <label>Threads:</label>
      <input type="number" id="threads" value="8" min="1" max="32">
      <button id="checkBtn" onclick="runCheck()">Check</button>
      <button class="secondary" onclick="clearAll()">Clear</button>
      <button class="secondary" onclick="exportCsv()">Export CSV</button>
    </div>
    <div class="status-bar" id="statusBar">Ready.</div>
    <div class="error-msg" id="errorMsg"></div>
  </div>

  <div class="card">
    <table id="resultsTable">
      <thead>
        <tr>
          <th>URL</th><th>Status</th><th>Code</th><th>Response Time</th>
          <th>Final URL</th><th>Error</th>
        </tr>
      </thead>
      <tbody id="resultsBody"></tbody>
    </table>
    <div class="summary" id="summary"></div>
  </div>

  <div class="card" style="font-size:12px;color:#6b7280;">
    <b>API usage:</b><br>
    <code>POST /api/check</code> with JSON body
    <code>{"urls":["https://example.com"],"timeout":10,"threads":8}</code><br>
    <code>GET /api/check?url=https://example.com&timeout=10</code><br>
    <code>GET /api/check/csv?url=https://example.com</code> (downloads CSV)<br>
    <code>GET /health</code> returns <code>{"status":"ok"}</code>
  </div>
</div>

<script>
let lastResults = [];

async function runCheck() {
  const urlText = document.getElementById('urls').value;
  const urls = urlText.split('\\n').map(s => s.trim()).filter(Boolean);
  if (!urls.length) { alert('Enter at least one URL.'); return; }

  const timeout = parseInt(document.getElementById('timeout').value) || 10;
  const threads = parseInt(document.getElementById('threads').value) || 8;
  const btn = document.getElementById('checkBtn');
  const status = document.getElementById('statusBar');
  const err = document.getElementById('errorMsg');
  err.textContent = '';
  btn.disabled = true; btn.textContent = 'Checking...';
  status.textContent = 'Checking ' + urls.length + ' URL(s)...';

  try {
    const resp = await fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls, timeout, threads})
    });
    const data = await resp.json();
    if (!resp.ok) {
      err.textContent = data.error || 'Request failed';
      return;
    }
    lastResults = data.results;
    renderResults(data.results, data.summary);
    status.textContent = 'Done. Total: ' + data.summary.total +
      '  |  Live: ' + data.summary.live +
      '  |  Warning: ' + data.summary.warning +
      '  |  Dead: ' + data.summary.dead;
  } catch (e) {
    err.textContent = 'Network error: ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Check';
  }
}

function renderResults(results, summary) {
  const tbody = document.getElementById('resultsBody');
  tbody.innerHTML = '';
  results.forEach(r => {
    const tr = document.createElement('tr');
    tr.className = r.status;
    tr.innerHTML =
      '<td>' + escapeHtml(r.url) + '</td>' +
      '<td><span class="pill ' + r.status + '">' + r.status + '</span></td>' +
      '<td>' + escapeHtml(r.code) + '</td>' +
      '<td>' + escapeHtml(r.response_time) + '</td>' +
      '<td>' + escapeHtml(r.final_url) + '</td>' +
      '<td>' + escapeHtml(r.error) + '</td>';
    tbody.appendChild(tr);
  });
  const s = document.getElementById('summary');
  s.innerHTML = '<span>Total: <b>' + summary.total + '</b></span>' +
    '<span>Live: <b style="color:#166534">' + summary.live + '</b></span>' +
    '<span>Warning: <b style="color:#854d0e">' + summary.warning + '</b></span>' +
    '<span>Dead: <b style="color:#991b1b">' + summary.dead + '</b></span>';
}

function clearAll() {
  document.getElementById('resultsBody').innerHTML = '';
  document.getElementById('summary').innerHTML = '';
  document.getElementById('statusBar').textContent = 'Cleared.';
  lastResults = [];
}

function exportCsv() {
  const urlText = document.getElementById('urls').value;
  const urls = urlText.split('\\n').map(s => s.trim()).filter(Boolean);
  if (!urls.length) { alert('Enter URLs first.'); return; }
  const timeout = parseInt(document.getElementById('timeout').value) || 10;
  const threads = parseInt(document.getElementById('threads').value) || 8;
  const params = new URLSearchParams();
  urls.forEach(u => params.append('url', u));
  params.set('timeout', timeout);
  params.set('threads', threads);
  window.location.href = '/api/check/csv?' + params.toString();
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry point (used when running locally; Railway uses gunicorn via Procfile)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
