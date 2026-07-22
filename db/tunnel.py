"""
Tunnel Connection Manager — Railway-side on-demand cloudflared access clients.

The hotel-side cloudflared runs PERMANENTLY as a Windows service, forwarding
the PMS database port through an outbound tunnel. This module manages the
Railway-side counterpart: short-lived `cloudflared access tcp` client
processes, opened per fetch and closed immediately after.

Responsibilities (per pilot spec):
  - local port allocation from a fixed pool (no collisions between workers)
  - global concurrency cap (TUNNEL_CONCURRENCY, default 5)
  - per-hostname single-flight (one live client per hotel at a time)
  - readiness health-check before handing the port to pyodbc
  - guaranteed process cleanup (context manager + atexit sweep)
  - service-token credentials passed via environment, never on the command
    line and never logged

Usage:
    from db.tunnel import manager
    with manager.acquire("sql-pome.hbis.io", client_id, client_secret) as port:
        conn = connect_mssql("127.0.0.1", port, user, password)
"""

import atexit
import os
import socket
import subprocess
import threading
import time


class TunnelError(RuntimeError):
    pass


_PORT_POOL = range(14330, 14400)


def _port_listening(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class TunnelManager:
    def __init__(self, max_tunnels: int | None = None):
        cap = max_tunnels or int(os.getenv("TUNNEL_CONCURRENCY", "5"))
        self._sem = threading.Semaphore(cap)
        self._state_lock = threading.Lock()
        self._used_ports: set[int] = set()
        self._host_locks: dict[str, threading.Lock] = {}
        self._procs: dict[int, subprocess.Popen] = {}
        atexit.register(self.shutdown)

    # ── internals ─────────────────────────────────────────────────────────

    def _host_lock(self, hostname: str) -> threading.Lock:
        with self._state_lock:
            return self._host_locks.setdefault(hostname, threading.Lock())

    def _alloc_port(self) -> int:
        with self._state_lock:
            for p in _PORT_POOL:
                if p not in self._used_ports and not _port_listening(p, 0.2):
                    self._used_ports.add(p)
                    return p
        raise TunnelError("no free local ports in the tunnel pool")

    def _free_port(self, port: int) -> None:
        with self._state_lock:
            self._used_ports.discard(port)
            self._procs.pop(port, None)

    def _build_cmd(self, hostname: str, port: int) -> list[str]:
        # Overridable in tests (fake listener instead of cloudflared)
        return ["cloudflared", "access", "tcp",
                "--hostname", hostname,
                "--url", f"127.0.0.1:{port}"]

    # ── public API ────────────────────────────────────────────────────────

    def acquire(self, hostname: str, client_id: str = "", client_secret: str = "",
                startup_timeout: float = 15.0) -> "_Tunnel":
        return _Tunnel(self, hostname, client_id, client_secret, startup_timeout)

    def shutdown(self) -> None:
        """Kill any orphaned tunnel processes (atexit safety net)."""
        with self._state_lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass


class _Tunnel:
    """Context manager for one live tunnel client."""

    def __init__(self, mgr: TunnelManager, hostname: str, client_id: str,
                 client_secret: str, startup_timeout: float):
        self._mgr = mgr
        self._hostname = hostname
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = startup_timeout
        self._port: int | None = None
        self._proc: subprocess.Popen | None = None
        self._host_lock: threading.Lock | None = None
        self._sem_held = False

    def __enter__(self) -> int:
        self._mgr._sem.acquire()          # global cap
        self._sem_held = True
        self._host_lock = self._mgr._host_lock(self._hostname)
        self._host_lock.acquire()         # single-flight per hotel
        try:
            self._port = self._mgr._alloc_port()

            env = dict(os.environ)
            if self._client_id:
                # cloudflared reads Access service tokens from these env vars —
                # keeps secrets off the command line and out of process lists
                env["TUNNEL_SERVICE_TOKEN_ID"] = self._client_id
                env["TUNNEL_SERVICE_TOKEN_SECRET"] = self._client_secret

            self._proc = subprocess.Popen(
                self._mgr._build_cmd(self._hostname, self._port),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._mgr._state_lock:
                self._mgr._procs[self._port] = self._proc

            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise TunnelError(
                        f"cloudflared exited (code {self._proc.returncode}) "
                        f"for {self._hostname}")
                if _port_listening(self._port, 0.5):
                    return self._port
                time.sleep(0.3)
            raise TunnelError(
                f"tunnel to {self._hostname} not ready after {self._timeout}s")
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, *exc) -> bool:
        self._cleanup()
        return False

    def _cleanup(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None
        if self._port is not None:
            self._mgr._free_port(self._port)
            self._port = None
        if self._host_lock is not None and self._host_lock.locked():
            try:
                self._host_lock.release()
            except RuntimeError:
                pass
            self._host_lock = None
        if self._sem_held:
            self._mgr._sem.release()
            self._sem_held = False


# Module-level singleton — one manager per process
manager = TunnelManager()
