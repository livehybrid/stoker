#!/usr/bin/env python3
"""Materialise splunk/security_content detections' ``attack_data`` as Stoker
rawreplay (Piston) packs.

Each security_content detection carries one or more test datasets — real,
recorded telemetry (`tests[].attack_data[]`, hosted on
``media.githubusercontent.com``) that the detection is written to fire on. This
tool turns those datasets into Stoker rawreplay packs: point Stoker at one and
replay the exact attack telemetry behind a chosen detection at a target, so the
detection can be exercised end to end.

It reads a **local security_content checkout** (clone splunk/security_content and
pass ``--checkout``) and writes one pack directory per dataset under ``--out``.
No dataset is downloaded here: the pack records the ``dataset_url``, and Stoker's
own rawreplay build fetches it through the existing SSRF-safe, size-capped https
path when a run launches. The emitted packs are registered like any other pack
directory (git-sync a repo of them, ``POST /api/packs`` with the path, or
``STOKER_BUILTIN_PACKS_DIR``).

Usage:
    python tools/security_content_packs.py --checkout ./security_content --out ./sc-packs
    python tools/security_content_packs.py --checkout ./security_content --out ./sc-packs \\
        --subset T1003,lsass,credential --limit 25 --index attack

Replay mode: the engine mode follows the RUN's pacing, not the pack — launch a
spec with ``rate_mode=count_interval`` for a single faithful pass (recorded
cadence, re-stamped to now), or ``eps`` to loop the capture at a fixed rate. The
pack's declared ``mode`` is advisory (see docs/PACKS.md).

Requires PyYAML (``pip install pyyaml``) for the checkout parse; the pack-writing
half is pure stdlib.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

# Fixed tags every materialised pack carries, ahead of the detection's MITRE ids.
FIXED_TAGS = ("security-content", "attack-data", "replay", "piston")
_MAX_DESC = 240


# --------------------------------------------------------------------------- #
# Parse a security_content checkout -> dataset specs (needs PyYAML).
# --------------------------------------------------------------------------- #

def parse_security_content(checkout_dir, subset=None):
    # type: (str, Optional[Sequence[str]]) -> List[Dict[str, Any]]
    """Walk a security_content checkout and return one spec per attack_data set.

    Prefers the ``detections/`` subtree (where the real detections live) and
    falls back to the whole checkout. ``subset`` (a list of case-insensitive
    substrings) keeps only specs whose detection name / MITRE id / data source
    matches any of them. PyYAML is imported here so importing this module (e.g.
    to reuse :func:`build_pack_yaml`) needs no yaml.
    """
    import yaml  # lazy: only the checkout parse needs it

    det_root = os.path.join(checkout_dir, "detections")
    root = det_root if os.path.isdir(det_root) else checkout_dir
    if not os.path.isdir(root):
        raise ValueError("not a directory: %s" % root)

    specs = []  # type: List[Dict[str, Any]]
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if not fn.endswith((".yml", ".yaml")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
            except Exception:  # a malformed/binary YAML must not stop the walk
                continue
            if isinstance(doc, dict):
                specs.extend(_specs_from_detection(doc))

    if subset:
        needles = [s.strip().lower() for s in subset if s.strip()]
        specs = [s for s in specs if _matches(s, needles)]
    # Stable order (detection then dataset) so a re-run is deterministic.
    specs.sort(key=lambda s: (s["detection"].lower(), s["dataset_url"]))
    return specs


def _specs_from_detection(doc):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    name = doc.get("name")
    tests = doc.get("tests")
    if not isinstance(name, str) or not name.strip() or not isinstance(tests, list):
        return []
    mitre = doc.get("mitre_attack_id") or []
    if isinstance(mitre, str):
        mitre = [mitre]
    mitre = [str(m).strip() for m in mitre if str(m).strip()]
    description = _one_line(str(doc.get("description") or ""))

    out = []  # type: List[Dict[str, Any]]
    seen = set()  # type: set
    for test in tests:
        if not isinstance(test, dict):
            continue
        for ad in (test.get("attack_data") or []):
            if not isinstance(ad, dict):
                continue
            url = str(ad.get("data") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "detection": name.strip(),
                "detection_id": doc.get("id"),
                "description": description,
                "mitre": mitre,
                "data_source": doc.get("data_source"),
                "dataset_url": url,
                "source": str(ad.get("source") or "").strip() or None,
                "sourcetype": str(ad.get("sourcetype") or "").strip() or None,
            })
    return out


def _matches(spec, needles):
    # type: (Dict[str, Any], Sequence[str]) -> bool
    hay = " ".join([
        spec["detection"], " ".join(spec.get("mitre") or []),
        str(spec.get("data_source") or ""), spec["dataset_url"],
        str(spec.get("source") or ""), str(spec.get("sourcetype") or ""),
    ]).lower()
    return any(n in hay for n in needles)


# --------------------------------------------------------------------------- #
# Build pack directories (pure stdlib — no yaml).
# --------------------------------------------------------------------------- #

def validate_spec(spec):
    # type: (Dict[str, Any]) -> List[str]
    """Essential rawreplay checks, mirroring server.bundles.parse_replay_config:
    an https dataset_url to a public host. Returns human-readable errors."""
    errors = []  # type: List[str]
    url = spec.get("dataset_url") or ""
    if not url.lower().startswith("https://"):
        errors.append("dataset_url must be https://, got %r" % url)
    else:
        host = urlsplit(url).hostname or ""
        import ipaddress
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None  # a hostname; Stoker re-resolves + checks at fetch time
        if addr is not None and (not addr.is_global or addr.is_multicast):
            errors.append("dataset_url host %s is not a public address" % host)
    return errors


def build_pack_yaml(spec, name, mode="cadence", time_multiple=1.0, index="main"):
    # type: (Dict[str, Any], str, str, float, str) -> str
    """Render a rawreplay pack.yaml for one dataset spec (single-line values,
    per the worker's flat two-level YAML subset)."""
    tags = ", ".join(list(FIXED_TAGS) + list(spec.get("mitre") or []))
    desc = spec.get("description") or spec["detection"]
    lines = [
        "# Materialised from splunk/security_content by",
        "# tools/security_content_packs.py — replays the detection's attack_data.",
        "# Detection: %s" % spec["detection"],
    ]
    if spec.get("detection_id"):
        lines.append("# Detection id: %s" % spec["detection_id"])
    lines += [
        "name: %s" % name,
        "tags: %s" % tags,
        "engine: rawreplay",
        "description: %s" % _yaml_quote(desc),
        "replay:",
        "  dataset_url: %s" % spec["dataset_url"],
        "  mode: %s" % mode,
        "  time_multiple: %s" % _fmt_float(time_multiple),
        "defaults:",
        "  index: %s" % index,
    ]
    if spec.get("sourcetype"):
        lines.append("  sourcetype: %s" % _yaml_quote(spec["sourcetype"]))
    if spec.get("source"):
        lines.append("  source: %s" % _yaml_quote(spec["source"]))
    return "\n".join(lines) + "\n"


# Where the bundle build embeds a fetched dataset_url (server.bundles
# _FETCHED_DATASET_RELPATH); the fallback stanza reads the same file.
FETCHED_DATASET = "dataset/replay.dat"


def build_fallback_conf(name, time_multiple=1.0):
    # type: (str, float) -> str
    """The pack's default/eventgen.conf: one ``mode = replay`` stanza over the
    embedded dataset. The worker agent requires the file in every bundle;
    Piston never reads it (engine == rawreplay skips the conf-rewrite), and it
    is a faithful eventgen fallback if a run is ever forced onto eventgen."""
    return "\n".join([
        "# Materialised by tools/security_content_packs.py. Piston (engine: rawreplay)",
        "# replays the dataset from pack.yaml's replay: section and never reads this",
        "# file; it keeps the pack a well-formed eventgen pack (the agent requires",
        "# default/eventgen.conf) and is a mode = replay fallback over the same data.",
        "",
        "[%s]" % name,
        "mode = replay",
        "sampleFile = %s" % FETCHED_DATASET,
        "timeMultiple = %s" % _fmt_float(time_multiple),
        "",
    ])


def pack_name(spec, taken):
    # type: (Dict[str, Any], set) -> str
    """A unique, filesystem-safe pack name for a spec (``sc-<detection>``,
    disambiguated by dataset file stem then a counter)."""
    base = "sc-" + _slug(spec["detection"])
    cand = base
    if cand in taken:
        stem = _slug(os.path.splitext(os.path.basename(
            urlsplit(spec["dataset_url"]).path))[0])
        cand = ("%s-%s" % (base, stem)) if stem else base
    n = 1
    unique = cand
    while unique in taken:
        n += 1
        unique = "%s-%d" % (cand, n)
    taken.add(unique)
    return unique


def materialise(specs, out_dir, mode="cadence", time_multiple=1.0, index="main"):
    # type: (Sequence[Dict[str, Any]], str, str, float, str) -> Dict[str, Any]
    """Write a pack directory per valid spec under ``out_dir``. Returns a
    manifest ``{written: [...], skipped: [...]}``; an invalid spec is skipped
    (never a half-written pack)."""
    os.makedirs(out_dir, exist_ok=True)
    taken = set()  # type: set
    written, skipped = [], []
    for spec in specs:
        errs = validate_spec(spec)
        if errs:
            skipped.append({"detection": spec["detection"],
                            "dataset_url": spec.get("dataset_url"), "errors": errs})
            continue
        name = pack_name(spec, taken)
        pack_dir = os.path.join(out_dir, name)
        os.makedirs(pack_dir, exist_ok=True)
        with open(os.path.join(pack_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
            fh.write(build_pack_yaml(spec, name, mode=mode,
                                     time_multiple=time_multiple, index=index))
        os.makedirs(os.path.join(pack_dir, "default"), exist_ok=True)
        with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w", encoding="utf-8") as fh:
            fh.write(build_fallback_conf(name, time_multiple=time_multiple))
        written.append({"name": name, "detection": spec["detection"],
                        "dataset_url": spec["dataset_url"]})
    return {"written": written, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Small helpers.
# --------------------------------------------------------------------------- #

def _one_line(text):
    # type: (str) -> str
    return re.sub(r"\s+", " ", text).strip()


def _yaml_quote(text):
    # type: (str) -> str
    """Double-quote a scalar for the flat parser: strip the chars that break its
    quote-aware comment stripping (`"` and `#`), collapse whitespace, truncate."""
    clean = _one_line(str(text)).replace('"', "").replace("#", "")
    if len(clean) > _MAX_DESC:
        clean = clean[:_MAX_DESC - 1].rstrip() + "…"
    return '"%s"' % clean


def _fmt_float(value):
    # type: (float) -> str
    return ("%g" % float(value))


# --------------------------------------------------------------------------- #
# Curation: a representative, bounded set instead of all ~2,000 datasets.
# --------------------------------------------------------------------------- #

_TECHNIQUE_RE = re.compile(r"T\d{4}(?:\.\d{3})?")


def techniques_of(spec):
    # type: (Dict[str, Any]) -> List[str]
    return _TECHNIQUE_RE.findall(str(spec.get("mitre") or ""))


def curate(specs, techniques, per_technique=2):
    # type: (Sequence[Dict[str, Any]], Sequence[str], int) -> List[Dict[str, Any]]
    """Up to ``per_technique`` datasets for each ATT&CK technique id, in the
    given order. A technique id matches itself and its sub-techniques (T1021
    matches T1021.002). Each dataset is used once, and within a technique the
    sourcetypes not yet picked anywhere come first, so the set spans as many
    data sources as it can."""
    used_urls = set()  # type: set
    seen_st = {}  # type: Dict[str, int]
    out = []  # type: List[Dict[str, Any]]
    for tech in techniques:
        cands = [sp for sp in specs
                 if sp.get("dataset_url") not in used_urls
                 and any(t == tech or t.startswith(tech + ".") for t in techniques_of(sp))]
        picked = 0
        while cands and picked < per_technique:
            cands.sort(key=lambda sp: (seen_st.get(sp.get("sourcetype") or "", 0), sp["detection"]))
            sp = cands.pop(0)
            if sp.get("dataset_url") in used_urls:
                continue
            used_urls.add(sp["dataset_url"])
            st = sp.get("sourcetype") or ""
            seen_st[st] = seen_st.get(st, 0) + 1
            out.append(dict(sp, curated_for=tech))
            picked += 1
    return out


def dataset_size(url, timeout=20.0):
    # type: (str, float) -> Optional[int]
    """Content-Length from a HEAD request (redirects followed), or None."""
    import urllib.request
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "stoker-curate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length is not None else None
    except Exception:  # noqa: BLE001 - any failure means "unknown"
        return None


def _slug(text):
    # type: (str) -> str
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:60] or "pack"


def main(argv=None):
    # type: (Optional[Sequence[str]]) -> int
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkout", required=True,
                   help="path to a local splunk/security_content clone")
    p.add_argument("--out", required=True, help="output directory for pack dirs")
    p.add_argument("--subset", default=None,
                   help="comma-separated substrings (detection name / MITRE id / "
                        "data source); keep only matching datasets")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of packs written (after --subset)")
    p.add_argument("--mode", choices=("cadence", "rate"), default="cadence",
                   help="declared replay mode (advisory; the run's rate_mode wins)")
    p.add_argument("--time-multiple", type=float, default=1.0,
                   help="cadence gap multiplier (1.0 = real time)")
    p.add_argument("--index", default="main", help="default index in each pack")
    p.add_argument("--techniques", default=None,
                   help="comma-separated ATT&CK technique ids to curate a representative set "
                        "(sub-techniques match their parent); see --per-technique")
    p.add_argument("--per-technique", type=int, default=2,
                   help="datasets per technique with --techniques (distinct datasets, "
                        "new sourcetypes first)")
    p.add_argument("--max-bytes", type=int, default=None,
                   help="skip datasets whose HEAD Content-Length exceeds this (network)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be written, write nothing")
    args = p.parse_args(argv)

    try:
        specs = parse_security_content(args.checkout,
                                       subset=args.subset.split(",") if args.subset else None)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.techniques:
        techs = [t.strip().upper() for t in args.techniques.split(",") if t.strip()]
        if args.max_bytes:
            sizes = {}  # type: Dict[str, Optional[int]]

            def small(sp):
                url = sp["dataset_url"]
                if url not in sizes:
                    sizes[url] = dataset_size(url)
                return sizes[url] is not None and sizes[url] <= args.max_bytes

            # curate first on the cheap, then replace any oversized pick
            pool = list(specs)
            chosen = []  # type: List[Dict[str, Any]]
            for tech in techs:
                for sp in curate(pool, [tech], args.per_technique * 4):
                    if len([c for c in chosen if c["curated_for"] == tech]) >= args.per_technique:
                        break
                    if small(sp) and sp["dataset_url"] not in {c["dataset_url"] for c in chosen}:
                        chosen.append(dict(sp, dataset_bytes=sizes[sp["dataset_url"]]))
            specs = chosen
        else:
            specs = curate(specs, techs, args.per_technique)
    if args.limit is not None:
        specs = specs[:max(0, args.limit)]

    if not specs:
        print("no attack_data datasets matched", file=sys.stderr)
        return 1

    if args.dry_run:
        taken = set()  # type: set
        for spec in specs:
            errs = validate_spec(spec)
            flag = "SKIP" if errs else "  ok"
            print("%s  %-40s  %s" % (flag, pack_name(spec, taken) if not errs
                                     else spec["detection"], spec["dataset_url"]))
        print("\n%d dataset(s) matched (dry run; nothing written)" % len(specs))
        return 0

    manifest = materialise(specs, args.out, mode=args.mode,
                           time_multiple=args.time_multiple, index=args.index)
    for row in manifest["written"]:
        print("wrote %s  <-  %s" % (row["name"], row["detection"]))
    if manifest["skipped"]:
        print("\nskipped %d dataset(s):" % len(manifest["skipped"]), file=sys.stderr)
        for row in manifest["skipped"]:
            print("  %s: %s" % (row["detection"], "; ".join(row["errors"])),
                  file=sys.stderr)
    print("\n%d pack(s) written to %s" % (len(manifest["written"]), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
