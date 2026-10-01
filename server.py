"""Local Witchcraft generator API and dependency-free web app."""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from build_generator import decode_pob, encode_pob, mechanics_fingerprint, validate_calculation
from prompt_generator import generate
from ollama_service import DEFAULT_MODEL, models
from pob_engine import calculate_with_pob, engine_status, export_with_pob
from services import game_context, market_data, publish

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
GENERATED = DATA / "generated"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "4173"))
JOBS: dict[str, dict] = {}
MAX_FINISHED_JOBS = 50
BUILDS: dict[str, dict] = {}
LOCK = threading.RLock()
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


def public(build: dict) -> dict:
    return {key: value for key, value in build.items() if not key.startswith("_")}


def save(build: dict):
    GENERATED.mkdir(parents=True, exist_ok=True)
    target = GENERATED / (build["id"] + ".json")
    temp = GENERATED / (build["id"] + ".tmp")
    temp.write_text(json.dumps(build, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def load(build_id: str) -> dict | None:
    if not re.fullmatch(r"g[0-9a-f]{20}", build_id):
        return None
    with LOCK:
        if build_id in BUILDS:
            return BUILDS[build_id]
    path = GENERATED / (build_id + ".json")
    if not path.is_file():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("id") != build_id or mechanics_fingerprint(result["_xml"]) != result["_fingerprint"]:
            return None
        with LOCK:
            BUILDS[build_id] = result
        return result
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def set_job(job_id: str, **changes):
    with LOCK:
        JOBS[job_id].update(changes)


def run_job(job_id: str, request: dict):
    try:
        set_job(job_id, status="running")
        build = generate(request, ROOT, DATA, lambda stage: set_job(job_id, stage=stage))
        with LOCK:
            BUILDS[build["id"]] = build
        save(build)
        set_job(job_id, stage="Publishing and verifying the public PoB")
        try:
            build["shareUrl"] = publish(encode_pob(build["_xml"]), decode_pob,
                                        build["_fingerprint"], mechanics_fingerprint)
            build["shareStatus"] = "published"
        except Exception as exc:
            build["shareStatus"] = "failed"
            build["shareError"] = str(exc)[:400]
        save(build)
        set_job(job_id, status="complete" if build["shareStatus"] == "published" else "share_failed",
                stage="Complete" if build["shareStatus"] == "published" else "Build saved; sharing needs a retry",
                result=public(build))
    except Exception as exc:
        set_job(job_id, status="failed", stage="Generation failed", error=str(exc)[:1600])


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def trusted_request(self, *, mutation=False) -> bool:
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in allowed_hosts or (mutation and origin and origin not in {f"http://{name}" for name in allowed_hosts}):
            self.reply(403, {"error": "Use the local Witchcraft page to access this server"})
            return False
        return True

    def reply(self, status: int, value, content_type="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8") if isinstance(value, (dict, list)) else value
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Requests must use application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 16_384:
            raise ValueError("Request body is too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Request must be a JSON object")
        return value

    def do_GET(self):
        if not self.trusted_request():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                available_models = models()
                status = {"pob": engine_status(ROOT), "ollama": {"available": bool(available_models),
                          "models": available_models, "defaultModel": DEFAULT_MODEL}}
                try:
                    context = game_context()
                    market = market_data(context["league"])
                    status["game"] = {"league": context["league"], "treeVersion": context["treeVersion"],
                                      "officialRelease": context["officialRelease"], "divineChaos": market["divineChaos"],
                                      "marketUpdated": market["updated"]}
                except Exception as exc:
                    status["gameError"] = str(exc)[:400]
                self.reply(200, status); return
            if parsed.path == "/api/job":
                job_id = query.get("id", [""])[0]
                with LOCK:
                    job = JOBS.get(job_id)
                    snapshot = dict(job) if job else None
                self.reply(200, snapshot) if snapshot else self.reply(404, {"error": "Job not found"})
                return
            if parsed.path == "/api/build":
                build = load(query.get("id", [""])[0])
                self.reply(200, public(build)) if build else self.reply(404, {"error": "Build not found"})
                return
            if parsed.path == "/api/export":
                build = load(query.get("id", [""])[0])
                if not build:
                    self.reply(404, {"error": "Build not found"}); return
                self.reply(200, encode_pob(build["_xml"]), "text/plain; charset=utf-8"); return
            if parsed.path in STATIC:
                filename, content_type = STATIC[parsed.path]
                self.reply(200, (ROOT / filename).read_bytes(), content_type); return
            self.reply(404, {"error": "Not found"})
        except Exception:
            self.reply(500, {"error": "The local server could not complete this request"})

    def do_POST(self):
        if not self.trusted_request(mutation=True):
            return
        try:
            request = self.body()
            if self.path == "/api/generate":
                prompt = request.get("prompt")
                if not isinstance(prompt, str) or not 12 <= len(prompt.strip()) <= 2000:
                    raise ValueError("Describe the Witch build you want in 12 to 2,000 characters")
                available_models = models()
                if not available_models:
                    raise ValueError("Start Ollama and install a local model before generating")
                model = request.get("model") or DEFAULT_MODEL
                if model not in available_models:
                    raise ValueError("Choose an installed local model")
                request = {"prompt": prompt.strip(), "model": model}
                with LOCK:
                    if sum(job["status"] in {"queued", "running"} for job in JOBS.values()) >= 2:
                        self.reply(429, {"error": "Two generations are already running; retry shortly"}); return
                    # Keep only the most recent finished jobs; the UI polls a job
                    # while it runs and saved builds live on disk.
                    finished = [key for key, job in JOBS.items() if job["status"] not in {"queued", "running"}]
                    for key in finished[:-MAX_FINISHED_JOBS]:
                        del JOBS[key]
                    job_id = secrets.token_hex(12)
                    JOBS[job_id] = {"id": job_id, "status": "queued", "stage": "Queued"}
                threading.Thread(target=run_job, args=(job_id, request), daemon=True).start()
                self.reply(202, {"jobId": job_id}); return
            if self.path == "/api/share":
                build = load(str(request.get("id", "")))
                if not build:
                    self.reply(404, {"error": "Generated build not found"}); return
                if build.get("shareStatus") == "published":
                    self.reply(200, public(build)); return
                # Retry always recalculates the exact bytes being exported.
                xml = decode_pob(encode_pob(build["_xml"]))
                if mechanics_fingerprint(xml) != build["_fingerprint"]:
                    raise RuntimeError("Saved build mechanics changed; regenerate")
                # Repair saved minimal candidates from before normal PoB export.
                if not ET.fromstring(xml).findall("./Build/PlayerStat"):
                    calculation = export_with_pob(xml, ROOT, DATA)
                    xml = calculation.pop("xml")
                else:
                    calculation = calculate_with_pob(xml, ROOT, DATA)
                failed = [check for check in validate_calculation(calculation) if not check["passed"]]
                if failed:
                    raise RuntimeError("Saved export no longer passes validation; regenerate. " +
                                       "; ".join(check["reason"] for check in failed))
                build["_xml"] = xml
                build["_fingerprint"] = mechanics_fingerprint(xml)
                build["stats"] = calculation["stats"]
                build["pobVersion"] = calculation.get("version")
                try:
                    build["shareUrl"] = publish(encode_pob(xml), decode_pob,
                                                build["_fingerprint"], mechanics_fingerprint)
                    build["shareStatus"] = "published"
                    build.pop("shareError", None)
                    save(build)
                    self.reply(200, public(build))
                except Exception as exc:
                    build["shareStatus"] = "failed"
                    build["shareError"] = str(exc)[:400]
                    save(build)
                    self.reply(502, {"error": build["shareError"], "build": public(build)})
                return
            self.reply(404, {"error": "Unknown endpoint"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.reply(400, {"error": str(exc)[:500]})
        except (RuntimeError, urllib.error.URLError) as exc:
            self.reply(502, {"error": str(exc)[:500]})
        except Exception:
            self.reply(500, {"error": "The local server could not complete this request"})


if __name__ == "__main__":
    try:
        with LocalServer((HOST, PORT), Handler) as server:
            print(f"Witchcraft: http://{HOST}:{PORT}", flush=True)
            server.serve_forever()
    except OSError as exc:
        raise SystemExit(f"Cannot start Witchcraft on {HOST}:{PORT}. Another server may already be running: {exc}")
