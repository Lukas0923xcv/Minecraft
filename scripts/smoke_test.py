#!/usr/bin/env python3
"""Boot the built server headless in a temp dir and check it is healthy.

    python scripts/smoke_test.py dist/minecraft-server-*.zip [--load-test] [--summary FILE]

Steps
  1. unzip into a fresh temp dir, accept the EULA there only, disable
     pause-when-empty, pick a free port
  2. start with the flags from jvm-flags.txt (smaller heap), wait for "Done"
  3. send /async stats and /spark tps over stdin and wait for their output
  4. optional load test: force-load 64 chunks, summon a few thousand mobs,
     measure MSPT with /spark tps + /tick query, then run a Chunky pre-generation
     and record chunks/s
  5. send stop, then fail if
       - the server crashed, timed out, or exited non-zero
       - any expected mod is missing from the loaded-mod list
       - the log has ERROR/FATAL lines or mod-incompatibility / mixin-failure /
         threading-violation warnings (except the allowlist below)
       - the expected thread-pool lines (Async pool, ScalableLux on C2ME) are absent
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modrinth as mr  # noqa: E402

EXPECTED_MOD_IDS = ["fabric-api", "async", "c2me", "scalablelux", "lithium", "ferritecore", "vmp", "spark", "chunky", "perfdash"]

# Lines that mean "something is wrong" even at WARN level.
BAD_PATTERNS = [
    r"incompatib", r"\bconflict", r"Mod resolution", r"MixinApplyError", r"Mixin apply (for|failed)",
    r"InvalidMixinException", r"Could not execute entrypoint", r"Exception in server tick loop",
    r"Encountered an unexpected exception", r"from multiple threads", r"ConcurrentModificationException",
    r"Forcing .* to be disabled", r"deadlock",
]
# Expected, harmless lines.
ALLOW_PATTERNS = [
    # Async deliberately cancels these two C2ME race *detectors* (they would
    # flag Async's own intentional off-thread entity ticking).
    r"Cancelled mixin com\.ishland\.c2me\.fixes\.general\.threading_issues\.mixin\.asynccatchers\.MixinThreadedAnvilChunkStorage",
    r"Cancelled mixin com\.ishland\.c2me\.fixes\.worldgen\.threading_issues\.mixin\.threading_detections\.random_instances\.MixinWorld",
    # Sandboxes without access to Mojang's auth services (not an issue in CI or on a real host).
    r"Minecraft Services Discovery", r"minecraftservices\.com", r"Services are unavailable",
    # The load test deliberately overloads the server.
    r"Can't keep up!",
]
REQUIRED_LINES = {
    "Async thread pool started": r"Initialized Pool with \d+ threads",
    "ScalableLux runs on C2ME's executor": r"Lighting scaling is enabled in externally managed mode",
    "C2ME executor sized": r"Global Executor Parallelism: \d+ configured",
    "dashboard mod started": r"perfdash ready",
    "dashboard Shift+F hook applied": r"perfdash key combo hook active",
}
LOG_LINE = re.compile(r"^\[[\d:]+\] \[(?P<thread>[^/\]]+)/(?P<level>[A-Z]+)\]")


class Server:
    def __init__(self, cmd: list[str], cwd: Path, log: Path):
        self.lines: list[str] = []
        self.q: queue.Queue[str] = queue.Queue()
        self.logf = log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.logf.write(line)
            self.logf.flush()
            self.lines.append(line.rstrip("\n"))
            self.q.put(line)
        self.q.put(None)

    def send(self, command: str):
        print(f">>> {command}", flush=True)
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pattern: str, timeout: float, start: int = 0) -> re.Match | None:
        """Wait until a line at index >= start matches. Returns None on timeout or exit."""
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        idx = start
        while time.time() < deadline:
            while idx < len(self.lines):
                m = rx.search(self.lines[idx])
                idx += 1
                if m:
                    return m
            if self.proc.poll() is not None and idx >= len(self.lines):
                return None
            time.sleep(0.2)
        return None

    def command(self, cmd: str, expect: str, timeout: float = 30) -> re.Match | None:
        mark = len(self.lines)
        self.send(cmd)
        return self.wait_for(expect, timeout, mark)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def jvm_flags(heap: str) -> list[str]:
    """Flags from jvm-flags.txt (lines starting with '-'), with the heap replaced."""
    flags = []
    for line in (mr.ROOT / "jvm-flags.txt").read_text().splitlines():
        line = line.strip()
        if not line.startswith("-") or line.startswith(("-Xms", "-Xmx", "-jar")):
            continue
        flags += line.split()
    return [f"-Xms{heap}", f"-Xmx{heap}"] + flags


def set_property(path: Path, key: str, value: str):
    text = path.read_text()
    text, n = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    if n == 0:
        text += f"\n{key}={value}\n"
    path.write_text(text)


def prefetch_vanilla(server_dir: Path):
    """Place the vanilla jar where the Fabric launcher looks for it, sha1-checked
    against modpack.toml (so the test also proves the pinned vanilla jar)."""
    pack = mr.load_modpack()
    mc = pack["minecraft"]["version"]
    info = mr.mc_server_download(mc)
    if info["server_sha1"] != pack["minecraft"]["server_sha1"]:
        raise SystemExit("vanilla server sha1 in Mojang manifest differs from modpack.toml")
    data = mr.download(info["server_url"])
    if mr.sha1(data) != pack["minecraft"]["server_sha1"]:
        raise SystemExit("vanilla server jar sha1 mismatch")
    target = server_dir / ".fabric" / "server" / f"{mc}-server.jar"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def dashboard_print(srv: Server) -> list[str]:
    """Run /dashboard print and return the panel lines (proves the sampler + renderer work)."""
    mark = len(srv.lines)
    srv.send("dashboard print")
    srv.wait_for(r"entities .* chunks", 20, mark)
    time.sleep(0.5)
    return [LOG_LINE.sub("", l).replace("System chat:", "").strip() for l in srv.lines[mark:]
            if LOG_LINE.match(l) and "Server thread/INFO" in l]


def load_test(srv: Server, report: list[str], entities: int, chunky_radius: int):
    report += ["", "## Load test", ""]
    srv.command("time set day", r"Set the time")
    srv.command("forceload add -64 -64 63 63", r"(?i)forceload|marked|chunks", 60)
    time.sleep(5)
    kinds = ["minecraft:villager", "minecraft:cow", "minecraft:sheep", "minecraft:pig", "minecraft:chicken"]
    side = 112  # blocks, inside the force-loaded 128x128 area
    per_row = max(1, int(entities ** 0.5))
    t0 = time.time()
    for i in range(entities):
        x = -56 + (i % per_row) * side // per_row
        z = -56 + (i // per_row) * side // per_row
        srv.send(f"execute positioned {x} 0 {z} positioned over motion_blocking run summon {kinds[i % len(kinds)]} ~ ~ ~")
    srv.wait_for(r"Summoned new", 60, max(0, len(srv.lines) - 5))
    time.sleep(3)
    report.append(f"Summoned {entities} mobs ({', '.join(k.split(':')[1] for k in kinds)}) "
                  f"into 64 force-loaded chunks in {time.time() - t0:.1f}s; let them tick for 30s.")
    time.sleep(30)

    m = srv.command("async stats", r"Threads|threads|Entities", 20)
    stats = [l for l in srv.lines[-15:] if re.search(r"(?i)entit|thread|async", l)]
    tps_mark = len(srv.lines)
    srv.send("spark tps")
    srv.wait_for(r"(?i)tick durations|MSPT", 20, tps_mark)
    time.sleep(1)
    tps_lines = [LOG_LINE.sub("", l).replace("System chat:", "").strip() for l in srv.lines[tps_mark:] if "[⚡]" in l]
    q_mark = len(srv.lines)
    srv.send("tick query")
    time.sleep(2)
    tick_lines = [LOG_LINE.sub("", l).strip() for l in srv.lines[q_mark:] if re.search(r"(?i)tick|percentile|ms", l)]

    report += ["### Entity load", "", "```"] + [LOG_LINE.sub("", l).strip() for l in stats] + tps_lines + tick_lines + ["```"]
    report += ["", "In-game dashboard (`/dashboard print`) under entity load:", "", "```"] + dashboard_print(srv) + ["```"]

    # Chunk generation throughput (C2ME + ScalableLux)
    srv.send("kill @e[type=!minecraft:player]")
    srv.send("forceload remove all")
    time.sleep(3)
    srv.send(f"chunky radius {chunky_radius}")
    mark = len(srv.lines)
    t0 = time.time()
    srv.send("chunky start")
    m = srv.wait_for(r"Task finished for minecraft:overworld.*", 600, mark)
    report += ["", "### Chunk generation (Chunky)", ""]
    if m:
        dur = time.time() - t0
        line = LOG_LINE.sub("", srv.lines[[i for i in range(mark, len(srv.lines)) if "Task finished" in srv.lines[i]][0]]).strip()
        chunks = (2 * chunky_radius // 16 + 1) ** 2
        report += [f"Pre-generated radius {chunky_radius} blocks (~{chunks} chunks) in {dur:.0f}s "
                   f"= **~{chunks / dur:.0f} chunks/s**", "", "```", line, "```"]
        tps_mark = len(srv.lines)
        srv.send("spark tps")
        srv.wait_for(r"(?i)tick durations|MSPT", 20, tps_mark)
        time.sleep(1)
        report += ["", "MSPT during/after generation:", "", "```"] + \
                  [LOG_LINE.sub("", l).replace("System chat:", "").strip() for l in srv.lines[tps_mark:] if "[⚡]" in l] + ["```"]
    else:
        report.append("Chunky did not finish within 600s (see log).")


def analyse(server_dir: Path, lines: list[str]) -> list[str]:
    problems = []
    log = server_dir / "logs" / "latest.log"
    text = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    all_lines = text + [l for l in lines if not LOG_LINE.match(l)]  # stdout-only lines (stack traces, JVM warnings)

    crash = server_dir / "crash-reports"
    if crash.exists() and any(crash.iterdir()):
        problems.append(f"crash report written: {[p.name for p in crash.iterdir()]}")

    allow = [re.compile(p) for p in ALLOW_PATTERNS]
    bad = [re.compile(p, re.IGNORECASE) for p in BAD_PATTERNS]
    for line in all_lines:
        if any(a.search(line) for a in allow):
            continue
        m = LOG_LINE.match(line)
        level = m.group("level") if m else ""
        if level in ("ERROR", "FATAL"):
            problems.append(f"{level}: {line}")
        elif any(b.search(line) for b in bad):
            problems.append(f"suspicious: {line}")

    joined = "\n".join(text)
    loaded = set(re.findall(r"^\s*(?:\|--|-)\s*([a-z0-9_\-]+) ", joined, re.MULTILINE))
    for mod_id in EXPECTED_MOD_IDS:
        if mod_id not in loaded:
            problems.append(f"mod not loaded: {mod_id}")
    for what, rx in REQUIRED_LINES.items():
        if not re.search(rx, joined):
            problems.append(f"missing expected log line ({what}): /{rx}/")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", type=Path)
    ap.add_argument("--java", default=os.environ.get("JAVA_BIN", "java"))
    ap.add_argument("--heap", default="4G")
    ap.add_argument("--start-timeout", type=float, default=300)
    ap.add_argument("--load-test", action="store_true")
    ap.add_argument("--entities", type=int, default=3000)
    ap.add_argument("--chunky-radius", type=int, default=480)
    ap.add_argument("--prefetch-vanilla", action="store_true",
                    help="download+sha1-check the vanilla jar instead of letting the launcher do it")
    ap.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    ap.add_argument("--keep", action="store_true", help="keep the temp server dir")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="mc-smoke-"))
    server_dir = tmp / "server"
    with zipfile.ZipFile(args.zip) as zf:
        zf.extractall(server_dir)
    (server_dir / "eula.txt").write_text("eula=true\n")  # accepted in the temp copy only
    props = server_dir / "server.properties"
    set_property(props, "server-port", str(free_port()))
    set_property(props, "pause-when-empty-seconds", "-1")  # keep ticking with no players
    if args.prefetch_vanilla:
        prefetch_vanilla(server_dir)

    cmd = [args.java] + jvm_flags(args.heap) + ["-jar", "server.jar", "--nogui"]
    print("starting:", " ".join(cmd), flush=True)
    srv = Server(cmd, server_dir, tmp / "console.log")
    report = ["# Server smoke test", "", f"`{args.zip.name}`, {os.cpu_count()} cpus, heap {args.heap}", ""]
    problems: list[str] = []

    t0 = time.time()
    done = srv.wait_for(r"Done \(([\d.,]+)s\)! For help", args.start_timeout)
    if not done:
        problems.append(f"server did not reach 'Done' within {args.start_timeout:.0f}s (exit code {srv.proc.poll()})")
    else:
        report.append(f"Started in {done.group(1)}s (wall {time.time() - t0:.0f}s, includes first-run library download).")
        if not srv.command("async stats", r"Performance Statistics|Threads", 30):
            problems.append("/async stats produced no output")
        if not srv.command("spark tps", r"TPS from last", 30):
            problems.append("/spark tps produced no output")
        report += ["", "```"] + [LOG_LINE.sub("", l).replace("System chat:", "").strip()
                                 for l in srv.lines[-20:] if re.search(r"(?i)async|thread|entit|TPS|⚡", l)] + ["```"]
        time.sleep(2)  # let the dashboard sampler take two samples
        panel = dashboard_print(srv)
        if not any("Main tick" in l for l in panel) or not any("Entities" in l for l in panel):
            problems.append(f"/dashboard print did not render the panel: {panel}")
        report += ["", "Dashboard at idle (`/dashboard print`):", "", "```"] + panel + ["```"]
        st = srv.command("dashboard selftest", r"perfdash selftest OK.*", 20)
        if not st:
            problems.append("/dashboard selftest failed (sidebar packets do not serialize)")
        else:
            report += ["", st.group(0)]
        if args.load_test:
            load_test(srv, report, args.entities, args.chunky_radius)
        srv.send("stop")

    try:
        code = srv.proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        srv.proc.kill()
        code = srv.proc.wait()
        problems.append("server did not stop within 180s after 'stop'")
    if code != 0:
        problems.append(f"server exited with code {code}")

    problems += analyse(server_dir, srv.lines)
    report += ["", "## Result", ""]
    report += ["**PASS**"] if not problems else ["**FAIL**", ""] + [f"- `{p[:300]}`" for p in problems]
    text = "\n".join(report) + "\n"
    print("\n" + text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text)

    if args.keep or problems:
        print(f"server dir kept at {server_dir}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
