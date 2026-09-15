from __future__ import annotations
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, message: str, returncode: int = 1, output: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.output = output


def which(name: str) -> str | None:
    return shutil.which(name)


def shell_join(args: list[str]) -> str:
    """Quote argv for the platform shell used by ``run_shell_stream``.

    POSIX sh and Windows cmd.exe have incompatible quoting rules. In particular,
    ``shlex.quote`` emits single quotes, which cmd.exe does not treat as quoting.
    """
    parts = [str(x) for x in args]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _resolved_exec_args(args: list[str]) -> list[str]:
    """Resolve argv[0] and make Windows .cmd/.bat launchable without shell=True.

    Python's subprocess uses CreateProcess directly on Windows. Batch wrappers
    such as npm.cmd cannot be launched as native executables, so route only those
    through COMSPEC. Native .exe tools remain direct children.
    """
    if not args:
        raise ValueError("args vacío")
    argv = [str(x) for x in args]
    resolved = which(argv[0])
    if resolved:
        argv[0] = resolved
    if os.name == "nt" and Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or which("cmd.exe") or "cmd.exe"
        return [comspec, "/d", "/c", *argv]
    return argv


def run_capture(args: list[str], cwd: str | Path | None = None, input_text: str | None = None,
                timeout: int | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    launch_args = _resolved_exec_args(args)
    try:
        p = subprocess.run(
            launch_args,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=merged,
        )
    except subprocess.TimeoutExpired as e:
        out = ""
        if isinstance(e.stdout, bytes):
            out += e.stdout.decode("utf-8", errors="replace")
        elif e.stdout:
            out += str(e.stdout)
        if isinstance(e.stderr, bytes):
            out += "\n" + e.stderr.decode("utf-8", errors="replace")
        elif e.stderr:
            out += "\n" + str(e.stderr)
        raise CommandError(f"Timeout después de {timeout}s: {' '.join(map(str, args))}", 124, out) from e
    except OSError as e:
        # Doctor and other diagnostics must report a missing/broken executable,
        # not crash with FileNotFoundError. Mutating paths still fail when check=True.
        detail = f"{type(e).__name__}: {e}"
        if check:
            raise CommandError(f"No se pudo ejecutar: {' '.join(map(str, args))}: {detail}", 127, detail) from e
        return subprocess.CompletedProcess(args=list(args), returncode=127, stdout="", stderr=detail)
    if check and p.returncode != 0:
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        raise CommandError(f"Comando falló ({p.returncode}): {' '.join(map(str, args))}", p.returncode, out)
    return p


def _terminate_process_tree(p: subprocess.Popen) -> None:
    """Best-effort process-tree termination, including child dev servers.

    A plain ``Popen.kill`` is not enough on Windows when npm/playwright spawned
    cmd/node children. ``taskkill /T`` terminates the whole tree. On POSIX we
    start the command in its own session and kill the process group.
    """
    if p.poll() is not None:
        return
    if os.name == "nt":
        taskkill = which("taskkill") or "taskkill"
        subprocess.run(
            [taskkill, "/PID", str(p.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(0.25)
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if p.poll() is None:
        try:
            p.kill()
        except ProcessLookupError:
            pass


def run_shell_stream(command: str, cwd: str | Path, log_path: Path, timeout: int = 1800) -> int:
    """Run a shell command while streaming output with a real wall-clock timeout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_new_session = os.name != "nt"
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        p = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=start_new_session,
        )
        assert p.stdout is not None
        output_q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            try:
                for line in p.stdout:
                    output_q.put(line)
            finally:
                output_q.put(None)

        reader = threading.Thread(target=_reader, name=f"autopilot-stream-{p.pid}", daemon=True)
        reader.start()
        deadline = time.monotonic() + max(1, timeout)
        eof = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and p.poll() is None:
                _terminate_process_tree(p)
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                log.write(f"\n[TIMEOUT after {timeout}s; process tree terminated]\n")
                log.flush()
                drain_until = time.monotonic() + 1.0
                while time.monotonic() < drain_until:
                    try:
                        item = output_q.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        eof = True
                        break
                    print(item, end="")
                    log.write(item)
                reader.join(timeout=1)
                try:
                    p.stdout.close()
                except Exception:
                    pass
                return 124

            try:
                item = output_q.get(timeout=min(0.2, max(0.01, remaining)))
            except queue.Empty:
                if p.poll() is not None and (eof or not reader.is_alive()):
                    break
                continue

            if item is None:
                eof = True
                if p.poll() is not None:
                    break
                continue

            print(item, end="")
            log.write(item)
            log.flush()

        try:
            rc = p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(p)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            log.write("\n[TIMEOUT waiting for process exit; process tree terminated]\n")
            rc = 124
        reader.join(timeout=1)
        try:
            p.stdout.close()
        except Exception:
            pass
        return rc
