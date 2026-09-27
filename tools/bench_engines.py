#!/usr/bin/env python3
"""Benchmark the eventgen engines end to end through the real worker agent.

For each engine (the vendored Python eventgen and firebox) and each target
rate, runs `python -m stoker_agent` standalone against tools/hec_sink.py for a
fixed duration and reports what the sink actually received, plus the CPU the
whole worker process tree (agent + engine) burned. Optionally also runs
`firebox bench` for the raw templating ceiling without the agent.

Usage:
    tools/bench_engines.py --pack packs/web-access --rates 2000,10000,50000 \
        --duration 20 --out data/bench.json

Needs the worker deps (requests, prometheus_client, jinja2, dateutil) in the
interpreter that runs it, and the firebox binary on PATH or in FIREBOX_BIN.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def sink_stats(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/stats" % port, timeout=5) as r:
        return json.loads(r.read().decode())


def start_sink(port, token, kind="fast"):
    script = "fast_sink.py" if kind == "fast" else "hec_sink.py"
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tools" / script), "--port", str(port), "--token", token],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            sink_stats(port)
            return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("hec_sink did not come up")


def cpu_children():
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def run_agent(engine, pack, rate, duration, port, token, firebox_bin, threads):
    env = dict(os.environ)
    env.update({
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": str(pack),
        "STOKER_HEC_URL": "http://127.0.0.1:%d" % port,
        "STOKER_HEC_TOKEN": token,
        "STOKER_INDEX": "bench",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": str(rate),
        "STOKER_DURATION_S": str(duration),
        "STOKER_METRICS_PORT": "0",
        "STOKER_EVENTGEN_IMPL": engine,
        "PYTHONPATH": "worker:worker/engines/eventgen",
    })
    if firebox_bin:
        env["STOKER_FIREBOX_BIN"] = firebox_bin
    if threads:
        env["STOKER_FIREBOX_THREADS"] = str(threads)
    before = sink_stats(port)
    cpu0 = cpu_children()
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "stoker_agent"], cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=duration + 120,
    )
    wall = time.time() - t0
    cpu = cpu_children() - cpu0
    after = sink_stats(port)
    delivered = after["events"] - before["events"]
    nbytes = after["bytes"] - before["bytes"]
    return {
        "engine": engine,
        "target_eps": rate,
        "duration_s": duration,
        "wall_s": round(wall, 2),
        "delivered": delivered,
        "delivered_eps": round(delivered / duration, 1),
        "achieved_pct": round(100.0 * delivered / (rate * duration), 1),
        "bytes": nbytes,
        "cpu_s": round(cpu, 2),
        "cpu_cores_avg": round(cpu / wall, 2) if wall else None,
        "cpu_us_per_event": round(1e6 * cpu / delivered, 1) if delivered else None,
        "agent_rc": proc.returncode,
        "tail": proc.stdout.strip().splitlines()[-3:],
    }


def firebox_raw(firebox_bin, pack, seconds, threads):
    out = subprocess.run(
        [firebox_bin, "bench", str(Path(pack) / "default" / "eventgen.conf"), "--seconds", str(seconds), "--threads", str(threads)],
        cwd=str(pack), capture_output=True, text=True, timeout=seconds + 60,
    )
    line = [l for l in out.stdout.splitlines() if l.startswith("bench:")]
    res = {"threads": threads, "seconds": seconds}
    if line:
        for kv in line[0][len("bench: "):].split():
            k, v = kv.split("=", 1)
            res[k] = float(v) if "." in v else int(v)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default="packs/web-access")
    ap.add_argument("--rates", default="2000,5000,10000,20000,50000")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--engines", default="python,firebox")
    ap.add_argument("--threads", type=int, default=0, help="STOKER_FIREBOX_THREADS (0 = all cores)")
    ap.add_argument("--port", type=int, default=18089)
    ap.add_argument("--skip-raw", action="store_true")
    ap.add_argument("--sink", choices=("fast", "validating"), default="fast",
                    help="fast = count-only sink (never the bottleneck); validating = tools/hec_sink.py")
    ap.add_argument("--out", help="write JSON results here")
    args = ap.parse_args(argv)

    pack = (REPO / args.pack).resolve() if not os.path.isabs(args.pack) else Path(args.pack)
    firebox_bin = os.environ.get("FIREBOX_BIN") or shutil.which("firebox")
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    if "firebox" in engines and not firebox_bin:
        raise SystemExit("firebox binary not found: set FIREBOX_BIN or put it on PATH")
    rates = [int(r) for r in args.rates.split(",") if r.strip()]
    token = "bench-token"
    sink = start_sink(args.port, token, args.sink)
    results = {"pack": str(pack), "host": os.uname().nodename, "cpus": os.cpu_count(), "runs": [], "firebox_raw": []}
    try:
        for engine in engines:
            for rate in rates:
                print("== %s @ %d eps for %ds" % (engine, rate, args.duration), file=sys.stderr, flush=True)
                r = run_agent(engine, pack, rate, args.duration, args.port, token, firebox_bin, args.threads)
                print("   delivered %d (%.1f%%), cpu %.1fs (%.2f cores)" % (r["delivered"], r["achieved_pct"], r["cpu_s"], r["cpu_cores_avg"] or 0), file=sys.stderr, flush=True)
                results["runs"].append(r)
        if firebox_bin and not args.skip_raw:
            for threads in sorted({1, os.cpu_count() or 1}):
                print("== firebox raw bench, %d thread(s)" % threads, file=sys.stderr, flush=True)
                results["firebox_raw"].append(firebox_raw(firebox_bin, pack, 5, threads))
    finally:
        sink.terminate()
        try:
            sink.wait(5)
        except subprocess.TimeoutExpired:
            sink.kill()

    print(render(results))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
    return 0


def render(results):
    lines = ["| engine | target eps | delivered eps | achieved | CPU cores (avg) | CPU per event |", "|---|---|---|---|---|---|"]
    for r in results["runs"]:
        lines.append("| %s | %d | %s | %s%% | %s | %s us |" % (
            r["engine"], r["target_eps"], r["delivered_eps"], r["achieved_pct"], r["cpu_cores_avg"], r["cpu_us_per_event"]))
    if results["firebox_raw"]:
        lines.append("")
        lines.append("| firebox raw (no agent) | threads | eps | MB/s |")
        lines.append("|---|---|---|---|")
        for r in results["firebox_raw"]:
            lines.append("| %s | %d | %s | %s |" % (Path(results["pack"]).name, r["threads"], r.get("eps"), r.get("MB/s")))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
