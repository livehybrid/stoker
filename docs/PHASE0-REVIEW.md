# Stoker Phase 0 Worker — Adversarial Audit Review

> Audit of the committed Phase 0 worker (HEAD `10835e9`), 2026-07-11. Six
> dimension reviewers, each finding independently refuted-or-confirmed, then
> synthesis. 10 findings filed, 4 confirmed real (3 medium, 1 low), 6 refuted,
> 0 uncertain. No critical or high findings survived verification.
>
> **Status: all four confirmed findings resolved.** protocol_security#1,
> concurrency#4 and confrewrite#2 fixed in `d1d6d0c`; pacing#1 resolved in
> `402036a` (option A: eps mode strips the shaping maps). Kept here as the
> audit record.

## Verdict

The Phase 0 worker is solid enough to ship the gate. Nothing in the confirmed
findings blocks Phase 0: all four surviving issues explicitly spare the flatline
exit-test path (no rate maps, no file tokens, healthy HEC/control plane), so
`STOKER_RATE_MODE=eps STOKER_RATE_VALUE=100` for 120 s against `index=loadtest`
still meets the ±1% count, clean drain and sub-45 s SIGTERM requirements. What
the audit exposes is a cluster of correctness and liveness gaps that bite only
outside the exit test: shaped packs silently under-deliver in eps mode, one
shipped example pack loses its status-code substitution, the drain can overrun
45 s when both HEC and control plane are partitioned, and a worker can hang
forever pre-T0 if the control plane dies in a narrow window.

## Confirmed findings

| id | severity | file:line | title |
|---|---|---|---|
| pacing#1 | medium | worker/stoker_agent/confrewrite.py:181-193 | eps mode preserves hourOfDayRate but paces a flat bucket, so shaped packs under-deliver below -1% |
| concurrency#4 | medium | worker/stoker_agent/agent.py:387-418 | Drain worst case can exceed the 45 s SIGTERM budget when HEC and control plane are degraded |
| protocol_security#1 | medium | worker/stoker_agent/agent.py:202-220 | Dead-man not enforced while polling for release (pre-T0 hang if control plane dies) |
| confrewrite#2 | low | worker/stoker_agent/engine.py:90-98 | Engine launched with no cwd, so relative file-token paths silently fail to substitute |

## Medium findings in detail

**pacing#1 — shaped packs under-deliver in eps mode.** In eps mode the token
bucket gates delivery against a flat `owed = share_eps x (t - anchor)`, but the
conf rewrite forces `interval=1`, `count=round(share_eps x overdrive)` while
preserving `hourOfDayRate`/`dayOfWeekRate` verbatim (rule 5), and the vendored
rater multiplies that count by `hourOfDayRate[hour]`. So the engine only supplies
`share_eps` when `hourOfDayRate >= 1/overdrive` (about 0.87); the shipped apigw
pack has 18 of 24 hourly values below that, so during those hours the engine
starves the bucket and delivered EPS drops far below target, a sustained breach
of the ±1% contract for any shaped pack. This is a genuine internal contradiction
between rule 5 and the ±1% promise. Fix: pick one rule for eps mode and make it
explicit, either strip the `*Rate` maps in eps rewrite (as `randomizeCount`
already is), or pre-scale each stanza's count by `1/min(rateFactor)` over the run
window, or amend the contract so eps mode is documented to ignore rate maps.

**concurrency#4 — drain can overrun the 45 s budget.** `_shutdown` runs its
stages sequentially with independent, additive timeouts and no global drain
deadline or outer watchdog. On a shared-egress partition affecting both the
remote HEC and the remote control plane, `flush_and_stop(20)` burns about 24 s
(senders stuck in 10 s POST timeouts that ignore `_abort`) and `control.final()`
burns about 33.5 s (three attempts at 10 s each plus sleeps), for roughly 57.5 s
before socket and engine, over the "exits 0 in < 45 s" requirement. The
consequence is the orchestrator SIGKILLs the pod mid-drain so the final POST
never lands (delivered counts are unaffected, no secret leaks). Fix: compute a
single drain deadline and clamp every stage, socket join, engine grace, HEC flush
and final, against remaining time, and set `_stopping` before `_sock.stop()` so
the redundant socket join is not double-counted.

**protocol_security#1 — dead-man not enforced during release-wait.**
`_await_release` polls `control.heartbeat()` until a release/drain command
arrives, but `heartbeat()` swallows every transport failure into a `None` return
and the loop never calls `control.deadman_expired()` (unlike `_run_loop`, which
does). A worker that claims successfully but whose control plane then dies before
issuing release loops forever, broken only by SIGTERM, instead of draining and
exiting after `STOKER_DEADMAN_S`. In managed EKS this strands the pod and its
fleet slot indefinitely. Fix: in the `_await_release` loop, after the heartbeat
attempt add a dead-man guard mirroring `_run_loop`.

## Low findings

- **confrewrite#2** — `EngineRunner.start()` calls `Popen` with no `cwd` and the
  agent never `chdir`s, so eventgen resolves the apigw pack's
  `token.2.replacement = samples/status_codes.sample` via `os.path.abspath`
  against the container WORKDIR (`/app/samples/...`), which does not exist; the
  token is logged as missing and returned unreplaced. Events still generate at
  correct count and rate, so impact is confined to degraded status-code fidelity
  in one shipped example pack. Fix: pass `cwd=pack.pack_dir` into `Popen` (or
  absolutise relative file-token paths during rewrite) and add an apigw generate
  test asserting a substituted 3-digit status appears.

## Recommended before Phase 1

1. Resolve the eps-mode vs rate-map contradiction (pacing#1) and update rule 5 to
   match so shaped packs hold ±1%.
2. Replace per-stage drain timeouts with a single global drain deadline that
   clamps every stage, and make senders and `control.final()` honour the
   abort/deadline (concurrency#4).
3. Add the dead-man guard to `_await_release` so a pre-T0 control-plane failure
   self-evicts instead of hanging (protocol_security#1).
4. Launch the engine with `cwd=pack.pack_dir` (or absolutise file-token paths) so
   relative file tokens substitute (confrewrite#2).
5. Add regression coverage for the uncovered paths: a shaped-pack eps run
   asserting delivered EPS tracks the target, a degraded-HEC-plus-control drain
   asserting exit under 45 s, and an agent-level dead-man test during
   release-wait.
