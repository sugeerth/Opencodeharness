"""Run OpenCode and the checks inside a container: `--sandbox docker[:IMAGE]`.

Open Agent passes OpenCode `--auto`, so the agent can run any shell command. In a sandbox it runs
inside a Docker container that can see only the workspace (mounted at /work), a private home for
OpenCode's sessions, and any file an environment variable points at (read-only, e.g. a provider
config). The check, the setup command and the held-out grade run in the same image, so the toolchain
the agent used is the one that judges it.

Each workspace gets its own home directory that lives across the rounds of an attempt, which is
what lets `--session` continue the same OpenCode session from one `docker run` to the next.
Credentials from `opencode auth login` are copied into it, and provider keys (*_API_KEY and friends)
are passed through from the environment.

`openagent sandbox build` builds the default image (openagent-runner) from the Dockerfile beside
this module: Python 3.13, Node 22, OpenCode, git, pytest.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE = "openagent-runner:latest"
DOCKERFILE_DIR = Path(__file__).with_name("docker")

# Environment passed into the container when set on the host: provider credentials and settings.
PASS_PREFIXES = ("OPENCODE_", "ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "AZURE_", "AWS_", "GROQ_",
                 "MISTRAL_", "DEEPSEEK_", "OPENROUTER_", "XAI_", "TOGETHER_", "FIREWORKS_", "HF_")
PASS_EXACT = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              # CA bundles for TLS-intercepting proxies; the files they name are mounted too
              "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "CURL_CA_BUNDLE")
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "CURL_CA_BUNDLE")


@dataclass(frozen=True)
class Docker:
    image: str = DEFAULT_IMAGE
    network: str = "host"            # host: OpenCode can reach model servers on localhost; "none" cuts it off
    binary: str = "opencode"         # OpenCode inside the image
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def parse(cls, spec: str) -> "Docker":
        """'docker' or 'docker:IMAGE'."""
        kind, _, image = spec.partition(":")
        if kind != "docker":
            raise ValueError(f"unknown sandbox {spec!r}: use docker or docker:IMAGE")
        return cls(image=image or DEFAULT_IMAGE)

    def home_for(self, workspace: Path) -> Path:
        """A private, persistent home for one workspace (one attempt), outside the workspace itself."""
        key = hashlib.sha1(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
        home = Path(tempfile.gettempdir()) / "openagent-sandbox" / key
        auth_dst = home / ".local" / "share" / "opencode" / "auth.json"
        if not auth_dst.exists():
            auth_src = opencode_data_dir() / "auth.json"
            auth_dst.parent.mkdir(parents=True, exist_ok=True)
            if auth_src.is_file():
                shutil.copy2(auth_src, auth_dst)
        return home

    def wrap(self, cmd: list[str], workspace: Path, env: dict[str, str] | None = None,
             name: str | None = None, mounts: tuple[str, ...] = ()) -> list[str]:
        """`docker run` for `cmd` (whose first word is replaced by the image's binary when it is OpenCode)."""
        workspace = Path(workspace).resolve()
        home = self.home_for(workspace)
        args = ["docker", "run", "--rm", "--name", name or f"openagent-{uuid.uuid4().hex[:12]}",
                "--network", self.network, "-v", f"{workspace}:/work", "-w", "/work",
                "-v", f"{home}:/oa-home", "-e", "HOME=/oa-home", "-e", "PWD=/work"]
        if hasattr(os, "getuid") and os.getuid() != 0:
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        merged = {k: v for k, v in os.environ.items() if k.startswith(PASS_PREFIXES) or k in PASS_EXACT}
        merged.update(env or {})
        mounted = set()
        for k, v in sorted(merged.items()):
            if k in ("PWD", "HOME"):
                continue
            args += ["-e", f"{k}={v}"]
            # a variable that names a file (e.g. OPENCODE_CONFIG) needs that file inside the container too
            if v.startswith("/") and os.path.exists(v) and v not in mounted and not v.startswith(str(workspace)):
                args += ["-v", f"{v}:{v}:ro"]
                mounted.add(v)
        for m in mounts:
            if m not in mounted:
                args += ["-v", f"{m}:{m}:ro"]
                mounted.add(m)
        return args + list(self.extra_args) + [self.image] + cmd

    def wrap_shell(self, command: str, workspace: Path, name: str | None = None) -> list[str]:
        """A shell command in the container. Absolute host paths the command names (a task's held-out
        grade/ directory, a check.sh beside the task) are mounted read-only at the same place. Only
        commands get these mounts: OpenCode's own container never does, so the agent can't read the
        held-out tests."""
        # not a login shell: that would reset PATH from /etc/profile and lose toolchains the image added
        return self.wrap(["bash", "-c", command], workspace, name=name,
                         mounts=named_paths(command, Path(workspace).resolve()))

    @staticmethod
    def kill(name: str) -> None:
        """`docker run --rm` exits with the container only if the container stops; killing the client
        does not stop it. Remove it by name."""
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def opencode_data_dir() -> Path:
    """Where OpenCode keeps auth.json: $XDG_DATA_HOME/opencode, by default ~/.local/share/opencode."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode"


def available() -> str:
    """Empty if Docker is usable; else why not."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                           timeout=30)
    except subprocess.TimeoutExpired:
        return "the Docker daemon did not answer in 30s"
    return "" if p.returncode == 0 else f"the Docker daemon is not reachable: {p.stderr.strip()[-200:]}"


def build(image: str = DEFAULT_IMAGE, log=print) -> int:
    """Build the runner image. Host networking lets the build use a proxy listening on localhost, and a
    CA bundle named by NODE_EXTRA_CA_CERTS or SSL_CERT_FILE is passed in for TLS-intercepting proxies."""
    cmd = ["docker", "build", "--network", "host", "-t", image]
    for var in CA_VARS:
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            cmd += ["--secret", f"id=extraca,src={path}"]
            log(f"passing the CA bundle from ${var} into the build")
            break
    log(f"building {image} from {DOCKERFILE_DIR / 'Dockerfile'}")
    return subprocess.run(cmd + [str(DOCKERFILE_DIR)]).returncode


def named_paths(command: str, workspace: Path) -> tuple[str, ...]:
    """Existing absolute host paths a shell command mentions, outside the workspace, deepest first
    removed (mounting a directory covers what is inside it)."""
    import shlex
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    found = []
    for w in words:
        for part in w.split(":"):
            if not part.startswith("/"):
                continue
            p = Path(part.rstrip("/.") or "/")
            if p.parts[:2] in (("/", "work"), ("/", "oa-home")) or str(p) in ("/", "/tmp", "/usr", "/bin", "/dev"):
                continue
            if p.exists() and workspace != p and workspace not in p.parents and p not in workspace.parents:
                found.append(str(p))
    keep = [p for p in found if not any(q != p and Path(p).is_relative_to(q) for q in found)]
    return tuple(dict.fromkeys(keep))
