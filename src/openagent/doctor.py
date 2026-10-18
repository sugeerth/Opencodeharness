"""`openagent doctor`: is everything Open Agent needs in place, and if not, what to do about it.

Checks OpenCode (installed, the version Open Agent is tested with), credentials, the model (listed by
OpenCode; with --ping, one real one-line run), git, the Docker sandbox, and AgentDiff. Each line says
what was found and, when something is missing, the command that fixes it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

TESTED_OPENCODE = "1.19.3"
KEY_SUFFIXES = ("_API_KEY", "_ACCESS_KEY_ID", "_TOKEN")
PROVIDER_PREFIXES = ("ANTHROPIC", "OPENAI", "GOOGLE", "GEMINI", "AZURE", "AWS", "GROQ", "MISTRAL", "DEEPSEEK",
                     "OPENROUTER", "XAI", "TOGETHER", "FIREWORKS", "HF", "GITHUB", "OPENCODE")


@dataclass
class Check:
    name: str
    status: str          # ok, warn, fail, skip
    detail: str
    fix: str = ""

    @property
    def mark(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}[self.status]


def _run(cmd: list[str], timeout: int = 60, **kw) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return Check("python", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro}",
                 "" if ok else "Open Agent needs Python 3.11 or newer")


def check_opencode(binary: str) -> Check:
    path = shutil.which(binary)
    if not path:
        return Check("opencode", "fail", f"{binary} is not on PATH", f"npm i -g opencode-ai@{TESTED_OPENCODE}")
    code, out = _run([path, "--version"])
    version = out.splitlines()[-1].strip() if out else "?"
    if code != 0:
        return Check("opencode", "fail", f"{path} --version failed: {out[-200:]}", f"npm i -g opencode-ai@{TESTED_OPENCODE}")
    if version != TESTED_OPENCODE:
        return Check("opencode", "warn", f"{version} at {path} (Open Agent is tested with {TESTED_OPENCODE})",
                     f"npm i -g opencode-ai@{TESTED_OPENCODE} if runs misbehave")
    return Check("opencode", "ok", f"{version} at {path}")


def check_credentials() -> Check:
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    stored = []
    if auth.is_file():
        try:
            stored = sorted(json.loads(auth.read_text()))
        except ValueError:
            pass
    env = sorted(k for k in os.environ if k.endswith(KEY_SUFFIXES) and k.startswith(PROVIDER_PREFIXES)
                 and os.environ[k])
    if not stored and not env and not os.environ.get("OPENCODE_CONFIG"):
        return Check("credentials", "warn", "no `opencode auth login` credentials and no provider key in the environment",
                     "opencode auth login   (or export ANTHROPIC_API_KEY=..., or try offline: openagent sim examples/tasks)")
    parts = []
    if stored:
        parts.append("logged in: " + ", ".join(stored))
    if env:
        parts.append("environment: " + ", ".join(env))   # names only, never values
    if os.environ.get("OPENCODE_CONFIG"):
        parts.append(f"OPENCODE_CONFIG={os.environ['OPENCODE_CONFIG']}")
    return Check("credentials", "ok", "; ".join(parts))


def check_model(binary: str, model: str | None) -> Check:
    if not model:
        return Check("model", "skip", "no -m given (and $OPENAGENT_MODEL unset): OpenCode's default model is used",
                     "openagent doctor -m provider/model to check one")
    if not shutil.which(binary):
        return Check("model", "skip", "OpenCode is not installed")
    provider = model.split("/", 1)[0]
    code, out = _run([binary, "models", provider], timeout=90)
    listed = {ln.strip() for ln in out.splitlines()}
    if model in listed:
        return Check("model", "ok", f"{model} is listed by `opencode models {provider}`")
    if code != 0 or not listed:
        return Check("model", "warn", f"`opencode models {provider}` failed: {out[-200:]}",
                     "check the provider name and your credentials")
    near = sorted(m for m in listed if m.startswith(provider + "/"))[:5]
    return Check("model", "fail", f"{model} is not among the {len(listed)} models OpenCode lists for {provider}",
                 "try one of: " + ", ".join(near) if near else f"opencode models {provider}")


def check_ping(binary: str, model: str | None, timeout: int = 180) -> Check:
    """One real, tiny run: the model answers through OpenCode."""
    from .opencode import OpenCode
    if not shutil.which(binary):
        return Check("ping", "skip", "OpenCode is not installed")
    with tempfile.TemporaryDirectory(prefix="openagent-doctor-") as d:
        r = OpenCode(binary=binary, model=model, timeout=timeout).run("Reply with the single word OK. Do not use tools.", cwd=d)
    if r.timed_out:
        return Check("ping", "fail", f"no answer in {timeout}s", "check the network and the provider's status")
    if r.infra_failure or not r.ok or r.empty:
        why = (r.stats.errors[-1] if r.stats.errors else r.stderr.strip()[-200:]) or "no output"
        return Check("ping", "fail", f"the run failed: {why}", "opencode auth login, or check the model name")
    return Check("ping", "ok", f"answered {r.stats.last_text.strip()[:40]!r} in {r.seconds:.1f}s, "
                               f"{r.stats.total_tokens:,} tokens, ${r.stats.cost:.4f}")


def check_git() -> Check:
    code, out = _run(["git", "--version"]) if shutil.which("git") else (-1, "")
    return (Check("git", "ok", out) if code == 0 else
            Check("git", "warn", "git is not installed", "needed for `tasks from-git` and the UI's diffs of in-place runs"))


def check_docker(image: str = "openagent-runner:latest") -> Check:
    from .sandbox import available
    why = available()
    if why:
        fix = "install Docker to run agents in a container" if "not installed" in why else "start the Docker daemon"
        return Check("sandbox", "skip", f"--sandbox docker unavailable: {why}", fix)
    code, _ = _run(["docker", "image", "inspect", image])
    if code != 0:
        return Check("sandbox", "warn", f"Docker works, but the image {image} is not built", "openagent sandbox build")
    return Check("sandbox", "ok", f"Docker works and {image} is built")


def check_agentdiff() -> Check:
    path = shutil.which("agentdiff")
    if not path:
        return Check("agentdiff", "skip", "not installed: --agentdiff evaluations are unavailable",
                     "pip install git+https://github.com/sugeerth/Agenttracing")
    return Check("agentdiff", "ok", path)


def run(binary: str = "opencode", model: str | None = None, ping: bool = False) -> list[Check]:
    checks = [check_python(), check_opencode(binary), check_credentials(), check_model(binary, model)]
    if ping:
        checks.append(check_ping(binary, model))
    return checks + [check_git(), check_docker(), check_agentdiff()]


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"{c.mark} {c.name.ljust(width)}  {c.detail}")
        if c.fix and c.status != "ok":
            lines.append(f"  {' ' * width}  → {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append("ready" if not fails and not warns else
                 f"{fails} problem(s), {warns} warning(s)" if fails else f"ready, with {warns} warning(s)")
    return "\n".join(lines)
