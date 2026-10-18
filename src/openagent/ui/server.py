"""`openagent ui`: a local web front end for Open Agent.

Start runs, benchmarks, evolutions and the offline demo; watch them live; read the results, the
diffs and every step of every trace. Standard library only. The page and its API are served on
127.0.0.1, and because the API runs commands on this machine, every API request must carry the
session token embedded in the page and a localhost Host header, so another website open in the
same browser can neither call it (CSRF) nor reach it through DNS rebinding.

Jobs live in `<home>/jobs/<id>/` (default home: ~/.openagent/ui): job.json, the log, and whatever
the job wrote (traces, results.json, evolution.json, a copy of the workspace).
"""
import difflib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..agent import PRESETS, Strategy, solve, with_overrides
from ..bench import agentdiff_eval, run_bench, write_reports
from ..opencode import OpenCode
from ..tasks import discover
from ..traces import to_trajectory, write_trajectory
from ..verify import CACHE_DIRS

PAGE = Path(__file__).with_name("index.html")
EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "tasks"
REALWORLD = Path(__file__).resolve().parents[3] / "benchmarks" / "realworld-v1.json"
REALWORLD_GO = Path(__file__).resolve().parents[3] / "benchmarks" / "realworld-go-v1.json"
REALWORLD_JS = Path(__file__).resolve().parents[3] / "benchmarks" / "realworld-js-v1.json"
MAX_DIFF = 400_000
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" '
           b'fill="#0f6e5f"/><path d="M11 9l12 7-12 7z" fill="#fff"/></svg>')


@dataclass
class Job:
    id: str
    kind: str                       # run | bench | evolve | demo | import
    params: dict
    dir: Path                       # what the job wrote, or the folder it opened (import)
    home: Path                      # where job.json and the log live
    status: str = "queued"          # queued | running | passed | failed | done | error
    title: str = ""
    created: float = field(default_factory=time.time)
    finished: float | None = None
    summary: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def say(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        with open(self.home / "log.txt", "a") as f:
            f.write(msg + "\n")

    def meta(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "title": self.title,
                "created": self.created, "finished": self.finished, "summary": self.summary,
                "params": self.params, "dir": str(self.dir)}

    def save(self) -> None:
        (self.home / "job.json").write_text(json.dumps(self.meta(), indent=2))


class Store:
    def __init__(self, home: Path):
        self.home = home
        (home / "jobs").mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        for p in sorted((home / "jobs").glob("*/job.json")):
            try:
                m = json.loads(p.read_text())
            except ValueError:
                continue
            j = Job(m["id"], m["kind"], m.get("params", {}), Path(m["dir"]), p.parent, m["status"],
                    m.get("title", ""), m.get("created", 0), m.get("finished"), m.get("summary", {}))
            if j.status in ("queued", "running"):
                j.status = "error"
                j.summary["error"] = "the UI stopped while this job was running"
            log = p.parent / "log.txt"
            j.log = log.read_text().splitlines()[-2000:] if log.is_file() else []
            self.jobs[j.id] = j

    def new(self, kind: str, params: dict, title: str, directory: Path | None = None) -> Job:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        jhome = self.home / "jobs" / jid
        jhome.mkdir(parents=True)
        j = Job(jid, kind, params, directory or jhome, jhome, title=title)
        self.jobs[jid] = j
        j.save()
        return j

    def update(self, j: Job) -> None:
        j.save()

    def delete(self, jid: str) -> None:
        j = self.jobs.pop(jid, None)
        if j is not None:  # only the job's own folder; an opened results folder is never touched
            shutil.rmtree(j.home, ignore_errors=True)


# ---- job bodies ---------------------------------------------------------------------------------

def _oc(p: dict, env: dict | None = None) -> OpenCode:
    sandbox = None
    if p.get("sandbox"):
        from ..sandbox import Docker, available
        why = available()
        if why:
            raise ValueError(f"sandbox: {why}")
        sandbox = Docker.parse(p["sandbox"] if isinstance(p["sandbox"], str) else "docker")
    return OpenCode(binary=p.get("binary") or "opencode", model=p.get("model") or None,
                    agent=p.get("agent") or None, timeout=int(p.get("timeout") or 900), env=env, sandbox=sandbox)


def _strategy(p: dict) -> Strategy:
    if p.get("harness"):
        s = Strategy.from_dict(p["harness"] if isinstance(p["harness"], dict) else json.loads(Path(p["harness"]).read_text()))
    else:
        s = PRESETS[p.get("preset") or "openagent"]
    kw = {k: int(p[k]) for k in ("rounds", "attempts") if p.get(k) not in (None, "")}
    return with_overrides(s, **kw)


def _diff(original: Path, changed: Path, files: list[str]) -> str:
    out = []
    for rel in files[:200]:
        a, b = original / rel, changed / rel
        try:
            al = a.read_text().splitlines(keepends=True) if a.is_file() else []
            bl = b.read_text().splitlines(keepends=True) if b.is_file() else []
        except UnicodeDecodeError:
            out.append(f"Binary file {rel} changed\n")
            continue
        out += difflib.unified_diff(al, bl, f"a/{rel}", f"b/{rel}")
        if sum(map(len, out)) > MAX_DIFF:
            out.append("\n... diff clipped ...\n")
            break
    return "".join(out)


def _git_diff(repo: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), "diff", "--no-color"], capture_output=True, text=True, timeout=30)
        return p.stdout[:MAX_DIFF] if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_job(j: Job, p: dict, store: Store) -> None:
    src = Path(p["dir"]).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"not a directory: {src}")
    work = src
    if p.get("copy", True):
        work = j.dir / "workspace"
        j.say(f"copying {src} to the job's workspace (your folder is not changed)")
        shutil.copytree(src, work, symlinks=True, ignore=shutil.ignore_patterns(*CACHE_DIRS))
    else:
        j.say(f"working in place in {src}")
    strategy = _strategy(p)
    protect = [x.strip() for x in (p.get("protect") or "").split(",") if x.strip()]
    max_cost = float(p["max_cost"]) if p.get("max_cost") not in (None, "") else None
    out = solve(p["task"], work, p.get("check") or None, _oc(p), strategy, protect, log=j.say, max_cost=max_cost)
    rec = out.to_dict()
    (j.dir / "result.json").write_text(json.dumps(rec, indent=2))
    traj = to_trajectory(out, "task", p["task"], p.get("check") or None, 1, p.get("model") or None)
    write_trajectory(traj, j.dir / "traces")
    ch = out.applied_changes
    files = sorted({f for v in ch.values() for f in v})
    diff = _diff(src, work, files) if p.get("copy", True) else _git_diff(src)
    (j.dir / "diff.patch").write_text(diff)
    from ..summary import markdown
    (j.dir / "summary.md").write_text(markdown(out, p["task"], p.get("check") or None))
    s = out.stats
    j.summary = {"passed": out.passed, "stop": rec.get("stop"), "repairs": out.repairs, "files": files,
                 "tokens": s.total_tokens, "cost": round(s.cost, 6), "seconds": round(out.seconds, 1),
                 "error": out.error, "trace": f"traces/{traj['trace_id']}.json"}
    j.status = "passed" if out.passed else "failed"


def bench_job(j: Job, p: dict, store: Store, scripted: bool = False) -> None:
    from ..tasks import resolve
    tasks_dir = resolve(Path(p.get("tasks") or EXAMPLES).expanduser().resolve(), j.dir / "tasks", j.say)
    tasks = discover(tasks_dir)
    only = [x.strip() for x in (p.get("only") or "").split(",") if x.strip()]
    if only:
        tasks = [t for t in tasks if t.name in only]
    if scripted:
        tasks = [t for t in tasks if (tasks_dir / t.name / "sim.json").is_file()]
    if not tasks:
        raise ValueError(f"no tasks in {tasks_dir}")
    presets = p.get("presets") or (["baseline", "guided", "openagent"] if scripted else ["baseline", "openagent"])
    strategies = [with_overrides(PRESETS[x]) for x in presets]
    if p.get("harness"):
        strategies.append(_strategy({"harness": p["harness"]}))
    repeat, jobs = int(p.get("repeat") or 1), int(p.get("jobs") or 2)
    synthetic = None

    def go(oc: OpenCode):
        if j.kind == "evolve" or p.get("evolve"):
            from ..evolve import evolve
            base = _strategy(p) if p.get("harness") else PRESETS["openagent"]
            ledger = evolve(tasks, base, oc, j.dir, int(p.get("generations") or 5), repeat, jobs, j.say,
                            synthetic, bool(p.get("agentdiff")))
            j.summary = {"versions": [{"version": h["version"], "passing": sum(h["rates"].values()),
                                       "tasks": len(h["rates"])} for h in ledger["history"]],
                         "stopped": ledger.get("stopped"), "evolution": "evolution.json"}
            return
        results = run_bench(tasks, strategies, oc, repeat, jobs, j.say, j.dir / "traces", synthetic)
        ad = agentdiff_eval(j.dir / "traces", j.dir / "agentdiff", log=j.say) if p.get("agentdiff") else None
        write_reports(results, j.dir, {"model": oc.model, "tasks": len(tasks), "repeat": repeat,
                                       "strategies": [s.to_dict() for s in strategies], "synthetic": synthetic}, ad)
        from ..bench import summarize
        j.summary = {"strategies": [{"strategy": r["strategy"], "passed": r["passed"], "runs": r["runs"]}
                                    for r in summarize(results)], "results": "results.json"}

    if scripted:
        from ..cli import SYNTHETIC
        from ..sim import ScriptedModel
        synthetic = SYNTHETIC
        if shutil.which(p.get("binary") or "opencode") is None:
            raise ValueError("the demo runs the real OpenCode, and `opencode` was not found: npm i -g opencode-ai")
        with tempfile.TemporaryDirectory(prefix="openagent-sim-") as cfg, \
                ScriptedModel([tasks_dir / t.name for t in tasks], Path(cfg)) as sim:
            j.say(f"scripted model serving at {sim.url}; OpenCode will use {sim.MODEL}")
            go(_oc({**p, "model": sim.MODEL}, sim.env()))
    else:
        go(_oc(p))
    j.status = "done"


def start(j: Job, store: Store) -> None:
    def body():
        j.status = "running"
        store.update(j)
        try:
            if j.kind == "run":
                run_job(j, j.params, store)
            elif j.kind == "demo":
                bench_job(j, j.params, store, scripted=True)
            else:
                bench_job(j, j.params, store)
        except Exception as e:  # report every failure in the UI instead of losing it in a thread
            j.status = "error"
            j.summary["error"] = str(e)
            j.say("error: " + str(e))
            j.say(traceback.format_exc().strip().splitlines()[-1])
        j.finished = time.time()
        store.update(j)
    threading.Thread(target=body, daemon=True).start()


# ---- HTTP ---------------------------------------------------------------------------------------

def _inside(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if p != base.resolve() and base.resolve() not in p.parents:
        raise PermissionError("path escapes the job folder")
    return p


def make_handler(store: Store, token: str):
    class H(BaseHTTPRequestHandler):
        server_version = f"openagent-ui/{__version__}"

        def log_message(self, *a):
            pass

        # -- helpers
        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authed(self) -> bool:
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-OpenAgent-Token", ""), token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # -- routes
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            u = urlparse(self.path)
            if u.path in ("/favicon.svg", "/favicon.ico"):
                return self._send(200, FAVICON, "image/svg+xml")
            if u.path in ("/", "/index.html"):
                page = PAGE.read_text().replace("__OPENAGENT_TOKEN__", token).replace("__VERSION__", __version__)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._authed():
                return self._json({"error": "missing or wrong token"}, 403)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = u.path.strip("/").split("/")
            try:
                if parts == ["api", "doctor"]:
                    from ..doctor import run as doctor
                    checks = doctor(model=q.get("model") or None, ping=q.get("ping") == "1")
                    return self._json({"checks": [c.__dict__ | {"mark": c.mark} for c in checks]})
                if parts == ["api", "info"]:
                    return self._json({"version": __version__, "opencode": shutil.which("opencode"),
                                       "agentdiff": shutil.which("agentdiff"), "examples": str(EXAMPLES),
                                       "examples_exist": EXAMPLES.is_dir(), "presets": list(PRESETS),
                                       "realworld": str(REALWORLD) if REALWORLD.is_file() else None,
                                       "realworld_go": str(REALWORLD_GO) if REALWORLD_GO.is_file() else None,
                                       "realworld_js": str(REALWORLD_JS) if REALWORLD_JS.is_file() else None,
                                       "docker": not __import__("openagent.sandbox", fromlist=["available"]).available(),
                                       "home": str(store.home), "cwd": os.getcwd()})
                if parts == ["api", "jobs"]:
                    return self._json([j.meta() for j in sorted(store.jobs.values(), key=lambda j: -j.created)])
                if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                    j = store.jobs.get(parts[2])
                    if j is None:
                        return self._json({"error": "no such job"}, 404)
                    if len(parts) == 3:
                        since = int(q.get("since", 0))
                        with j.lock:
                            lines = j.log[since:]
                        return self._json({**j.meta(), "log": lines, "log_total": since + len(lines)})
                    if parts[3] == "file":
                        f = _inside(j.dir, q.get("path", ""))
                        if not f.is_file():
                            return self._json({"error": "no such file"}, 404)
                        if f.suffix == ".json":
                            return self._send(200, f.read_bytes())
                        return self._send(200, f.read_bytes()[:MAX_DIFF * 2], "text/plain; charset=utf-8")
                    if parts[3] == "files":
                        sub = _inside(j.dir, q.get("path", ""))
                        files = sorted(str(p.relative_to(j.dir)) for p in sub.rglob("*.json")
                                       if "workspace" not in p.relative_to(j.dir).parts)[:5000]
                        return self._json(files)
                return self._json({"error": "not found"}, 404)
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)

        def do_POST(self):
            if not self._authed():
                return self._json({"error": "missing or wrong token"}, 403)
            u = urlparse(self.path)
            try:
                p = self._body()
                if u.path == "/api/run":
                    if not p.get("task") or not p.get("dir"):
                        return self._json({"error": "a task and a directory are required"}, 400)
                    j = store.new("run", p, p["task"][:90])
                elif u.path == "/api/bench":
                    j = store.new("bench", p, f"bench {p.get('tasks') or 'examples'}")
                elif u.path == "/api/evolve":
                    j = store.new("evolve", p, f"evolve {p.get('tasks') or 'examples'}")
                elif u.path == "/api/demo":
                    j = store.new("demo", p, "demo: evolve the harness" if p.get("evolve")
                                  else "demo: baseline vs guided vs openagent")
                elif u.path == "/api/import":
                    d = Path(p.get("dir", "")).expanduser().resolve()
                    if not ((d / "results.json").is_file() or (d / "evolution.json").is_file()):
                        return self._json({"error": f"{d} has no results.json or evolution.json"}, 400)
                    j = store.new("import", {"dir": str(d)}, f"opened {d.name}", directory=d)
                    j.status = "done"
                    j.summary = {"results": "results.json"} if (d / "results.json").is_file() else {"evolution": "evolution.json"}
                    store.update(j)
                    return self._json(j.meta())
                elif u.path.startswith("/api/jobs/") and u.path.endswith("/delete"):
                    j = store.jobs.get(u.path.split("/")[3])
                    if j is not None and j.status in ("queued", "running"):
                        return self._json({"error": "the job is still running"}, 409)
                    store.delete(u.path.split("/")[3])
                    return self._json({"ok": True})
                else:
                    return self._json({"error": "not found"}, 404)
                start(j, store)
                return self._json(j.meta())
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)

    return H


def serve(port: int = 8765, home: str | Path | None = None, open_browser: bool = True) -> None:
    home = Path(home or Path.home() / ".openagent" / "ui").expanduser()
    store = Store(home)
    token = secrets.token_urlsafe(24)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token))
    except OSError as e:  # the port is taken (often by another `openagent ui`): take any free one
        print(f"port {port} is not available ({e.strerror}); using a free port instead", flush=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, token))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Open Agent UI: {url}  (jobs in {home}; Ctrl-C to stop)", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
