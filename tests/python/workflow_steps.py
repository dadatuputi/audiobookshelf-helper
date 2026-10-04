"""Run a step of .github/workflows/release.yml as written, outside Actions.

The release jobs' branching lives in shell inside the workflow, so testing a
copy of it would test the copy. This lifts the step's `run:` block and `env:`
names out of the file itself, so a test fails when the workflow changes under
it rather than passing against what it used to say.

It is a reader for the shape release.yml is written in - block-scalar `run: |`
under `- name:` steps under two-space-indented jobs - not a YAML parser. The
test suite is stdlib-only and CI does not install PyYAML; when the file stops
having that shape, `step()` raises rather than guessing.
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

# Actions runs a `run:` block with no `shell:` as `bash -e {0}`: errexit, and
# no pipefail - which is why the sign step reads PIPESTATUS itself.
BASH = shutil.which("bash")
CAN_RUN = os.name != "nt" and BASH is not None


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def job_lines(job, text=None):
    lines = (text if text is not None else WORKFLOW.read_text()).splitlines()
    for i, line in enumerate(lines):
        if line == f"  {job}:":
            out = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and _indent(nxt) <= 2:
                    break
                out.append(nxt)
            return out
    raise LookupError(f"no job {job!r} in {WORKFLOW.name}")


def job_text(job):
    return "\n".join(job_lines(job))


def step(job, name):
    """{'run': str, 'env': {NAME: raw value}, 'if': raw str or None, 'id': ...}"""
    lines = job_lines(job)
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)- name: (.+?)\s*$", line)
        if m and m.group(2).strip("\"'") == name:
            start, dash = i, len(m.group(1))
            break
    if start is None:
        raise LookupError(f"no step {name!r} in job {job!r}")
    body = []
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= dash:
            break
        body.append(line)
    key_indent = dash + 2
    out = {"run": None, "env": {}, "if": None, "id": None}
    i = 0
    while i < len(body):
        line = body[i]
        m = re.match(rf"^ {{{key_indent}}}([A-Za-z_-]+):\s*(.*)$", line)
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        block = []
        while i < len(body) and (not body[i].strip() or _indent(body[i]) > key_indent):
            block.append(body[i])
            i += 1
        if key == "run":
            if value != "|":
                raise ValueError(f"{name}: run is not a `|` block scalar")
            real = [b for b in block if b.strip()]
            cut = min(_indent(b) for b in real)
            out["run"] = "\n".join(b[cut:] for b in block).rstrip() + "\n"
        elif key == "env":
            for b in block:
                em = re.match(r"^\s+([A-Z_][A-Z0-9_]*):\s*(.*)$", b)
                if em:
                    out["env"][em.group(1)] = em.group(2)
        elif key in ("if", "id"):
            out[key] = value
    if out["run"] is None:
        raise LookupError(f"step {name!r} has no run block")
    return out


class Ran:
    def __init__(self, proc, outputs, cwd):
        self.code = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.outputs = outputs
        self.cwd = cwd

    @property
    def log(self):
        return self.stdout + self.stderr


def run_step(job, name, env, cwd, path_prepend=(), allow=()):
    """Run the step's script with `env` as its step env, in `cwd`.

    Only names the step declares under `env:` may be passed (plus `allow`, for
    a stub's own controls), so a renamed variable fails here instead of being
    quietly supplied by the test.
    """
    s = step(job, name)
    if "${{" in s["run"]:
        raise ValueError(f"{name}: the script interpolates an expression; it can "
                         f"only be run here if it reads it from env instead")
    undeclared = set(env) - set(s["env"]) - set(allow)
    if undeclared:
        raise AssertionError(f"{name} does not declare {sorted(undeclared)} in env:")
    cwd = Path(cwd)
    # mkstemp hands back an open descriptor as well as the name; close it, or
    # the file cannot be deleted afterwards on Windows (WinError 32).
    fd, name = tempfile.mkstemp(prefix="gh-output-")
    os.close(fd)
    outputs_file = Path(name)
    fd, name = tempfile.mkstemp(prefix="step-", suffix=".sh")
    os.close(fd)
    script = Path(name)
    script.write_text(s["run"])
    full = {
        "PATH": os.pathsep.join([*map(str, path_prepend), os.environ.get("PATH", "")]),
        "HOME": os.environ.get("HOME", str(cwd)),
        "GITHUB_OUTPUT": str(outputs_file),
        **{k: str(v) for k, v in env.items()},
    }
    for k in ("SYSTEMROOT", "TMPDIR", "LANG"):
        if k in os.environ:
            full.setdefault(k, os.environ[k])
    try:
        proc = subprocess.run([BASH, "-e", str(script)], cwd=str(cwd), env=full,
                              capture_output=True, text=True, timeout=300)
        outputs = {}
        for line in outputs_file.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                outputs[k] = v
    finally:
        outputs_file.unlink()
        script.unlink()
    return Ran(proc, outputs, cwd)
