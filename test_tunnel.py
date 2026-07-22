"""
Tunnel Connection Manager tests — no cloudflared needed.
Run: py -3 test_tunnel.py
A fake subclass spawns a local TCP listener instead of cloudflared, so port
allocation, readiness, single-flight, concurrency cap, and cleanup are all
exercised for real (real subprocesses, real sockets).
"""
import io
import socket
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from db.tunnel import TunnelManager, TunnelError, _port_listening

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


_LISTENER = ("import socket,sys,time\n"
             "s=socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(5)\n"
             "time.sleep(300)\n")


class FakeManager(TunnelManager):
    """Spawns a python TCP listener instead of cloudflared."""
    def _build_cmd(self, hostname, port):
        return [sys.executable, "-c", _LISTENER, str(port)]


class BrokenManager(TunnelManager):
    """Spawns a process that exits immediately (simulates cloudflared failure)."""
    def _build_cmd(self, hostname, port):
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


print("── 1. acquire → ready port → release → cleanup ──")
mgr = FakeManager(max_tunnels=5)
with mgr.acquire("sql-fake.hbis.io") as port:
    check("port in pool", 14330 <= port < 14400, str(port))
    check("port is listening", _port_listening(port))
    proc = mgr._procs.get(port)
    check("process tracked", proc is not None)
time.sleep(0.5)
check("process terminated after release", proc.poll() is not None)
check("port freed", port not in mgr._used_ports)

print("── 2. failed process → TunnelError, no leaks ──")
broken = BrokenManager(max_tunnels=5)
try:
    with broken.acquire("sql-fake.hbis.io", startup_timeout=5) as p:
        check("should not reach here", False)
except TunnelError as e:
    check("TunnelError raised", "exited" in str(e), str(e))
check("no ports leaked", len(broken._used_ports) == 0)
check("semaphore released", broken._sem.acquire(blocking=False) and (broken._sem.release() or True))

print("── 3. per-hotel single-flight ──")
mgr2 = FakeManager(max_tunnels=5)
order = []
t1_entered = threading.Event()

def first():
    with mgr2.acquire("sql-same.hbis.io") as p:
        order.append("first-in")
        t1_entered.set()
        time.sleep(1.5)
        order.append("first-out")

def second():
    t1_entered.wait()
    with mgr2.acquire("sql-same.hbis.io") as p:
        order.append("second-in")

t1 = threading.Thread(target=first)
t2 = threading.Thread(target=second)
t1.start(); t2.start(); t1.join(); t2.join()
check("second waits for first (single-flight)", order == ["first-in", "first-out", "second-in"], str(order))

print("── 4. global concurrency cap ──")
mgr3 = FakeManager(max_tunnels=1)
cap_order = []
a_entered = threading.Event()

def hotel_a():
    with mgr3.acquire("sql-a.hbis.io") as p:
        cap_order.append("a-in")
        a_entered.set()
        time.sleep(1.5)
        cap_order.append("a-out")

def hotel_b():
    a_entered.wait()
    with mgr3.acquire("sql-b.hbis.io") as p:   # different hotel, but cap=1
        cap_order.append("b-in")

ta = threading.Thread(target=hotel_a)
tb = threading.Thread(target=hotel_b)
ta.start(); tb.start(); ta.join(); tb.join()
check("cap=1 serializes different hotels", cap_order == ["a-in", "a-out", "b-in"], str(cap_order))

print("── 5. two hotels in parallel when cap allows ──")
mgr4 = FakeManager(max_tunnels=5)
ports = {}

def open_hold(name, hold_evt, ready_evt):
    with mgr4.acquire(name) as p:
        ports[name] = p
        ready_evt.set()
        hold_evt.wait(timeout=10)

hold = threading.Event()
r1, r2 = threading.Event(), threading.Event()
tx = threading.Thread(target=open_hold, args=("sql-x.hbis.io", hold, r1))
ty = threading.Thread(target=open_hold, args=("sql-y.hbis.io", hold, r2))
tx.start(); ty.start()
r1.wait(10); r2.wait(10)
check("both live simultaneously", len(ports) == 2 and ports["sql-x.hbis.io"] != ports["sql-y.hbis.io"], str(ports))
check("both ports listening", all(_port_listening(p) for p in ports.values()))
hold.set(); tx.join(); ty.join()

print()
print(f"{'ALL PASS' if failed == 0 else 'FAILURES'}: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
