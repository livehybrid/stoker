#!/usr/bin/env python3
"""Build eventgen packs from splunk/security_content ``data_sources/*.yml``.

Every data source in security_content (Apache-2.0) documents a Splunk data
source a detection relies on: its ``sourcetype``, ``source`` and, for most, an
``example_log`` (one real-shaped event). This tool runs each example through
the pack builder (``server.packbuilder``): the analyser picks the fields to
vary, the builder writes an ordinary eventgen pack, and a quality gate keeps
only packs that lint, render and actually vary.

    python tools/sc_datasource_packs.py --checkout ./security_content --out ./packs \\
        --report sc-report.json

Only the example events are copied (they are the Apache-2.0 content); the
packs name their source file in the description. Multi-line examples are kept
when a breaker splits the example back into exactly one event.

Quality gate, per pack:
  * the example fits the builder limits (64 KB);
  * at least one enabled field besides the timestamp was suggested;
  * the builder preview renders with no warning and changes the event;
  * the written pack passes the same lint as an uploaded pack.

Needs PyYAML and the Stoker server package on the path (run from the repo root).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from server import bundles  # noqa: E402
from server import packbuilder as pb  # noqa: E402

UPSTREAM = "https://github.com/splunk/security_content"
FIXED_TAGS = ["security-content", "data-source"]


def _slug(text):
    # type: (str) -> str
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _example(doc):
    # type: (Dict[str, Any]) -> Optional[str]
    ex = doc.get("example_log")
    if not isinstance(ex, str):
        return None
    ex = ex.replace("\r\n", "\n").strip("\n")
    return ex if ex.strip() else None


def _events(example):
    # type: (str) -> Dict[str, Any]
    """The example's events as the builder splits them: usually one line; a
    multi-line event keeps its breaker; several lines become several events;
    pretty-printed JSON is compacted."""
    if "\n" not in example:
        return {"events": [example], "breaker": None}
    split = pb.split_input(example, "auto")
    if split["format"] in ("csv", "splunk_csv"):
        split = pb.split_input(example, "line")
    return {"events": split["events"], "breaker": split["breaker"]}


def _tags(doc):
    # type: (Dict[str, Any]) -> List[str]
    tags = list(FIXED_TAGS)
    for comp in doc.get("mitre_components") or []:
        t = _slug(comp)[:40]
        if t and t not in tags:
            tags.append(t)
    return tags[:20]


def build_one(path, doc, out_dir):
    # type: (str, Dict[str, Any], str) -> Dict[str, Any]
    rel = os.path.relpath(path, os.path.dirname(os.path.dirname(path)))
    name = "sc-ds-" + _slug(doc.get("name") or os.path.basename(path)[:-4])[:57].strip("-")
    rec = {"file": rel, "name": name, "sourcetype": doc.get("sourcetype")}  # type: Dict[str, Any]
    example = _example(doc)
    if example is None:
        return dict(rec, status="skipped", reason="no example_log")
    if len(example.encode("utf-8")) > pb.MAX_EVENT_BYTES:
        return dict(rec, status="skipped", reason="example larger than %d KB" % (pb.MAX_EVENT_BYTES // 1024))
    split = _events(example)
    suggestions = pb.analyse(split["events"])["suggestions"]
    enabled = [s for s in suggestions if s["enabled"]]
    if not [s for s in enabled if s["replacement"]["kind"] != "timestamp"]:
        return dict(rec, status="rejected", reason="nothing to vary besides the timestamp")
    desc = str(doc.get("description") or doc.get("name") or "").strip()
    desc = "%s. Example event from security_content %s (Apache-2.0)." % (desc.rstrip("."), rel)
    cfg = pb.validate_config({
        "name": name,
        "description": desc,
        "sourcetype": doc.get("sourcetype"),
        "tags": _tags(doc),
        "events": split["events"],
        "breaker": split["breaker"],
        "tokens": suggestions,
        "count": 10,
        "interval": 1,
    })
    preview = pb.render_preview(cfg, n=10, seed=7)
    if preview["warnings"]:
        return dict(rec, status="rejected", reason="; ".join(preview["warnings"]))
    if all(ev == split["events"][0] for ev in preview["events"]):
        return dict(rec, status="rejected", reason="preview never changes the event")
    dest = os.path.join(out_dir, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    pb.write_pack(cfg, dest)
    source = str(doc.get("source") or "").strip()
    if source and "\n" not in source:
        yaml_path = os.path.join(dest, "pack.yaml")
        with open(yaml_path, encoding="utf-8") as fh:
            text = fh.read()
        if "\ndefaults:\n" in text:
            text = text.replace("\ndefaults:\n", "\ndefaults:\n  source: %s\n" % source, 1)
        else:
            text += "defaults:\n  source: %s\n" % source
        with open(yaml_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    lint = bundles.lint_pack(dest)
    if not lint.ok:
        shutil.rmtree(dest)
        return dict(rec, status="rejected", reason="lint: %s" % "; ".join(lint.errors))
    return dict(rec, status="ok", fields=len(enabled), breaker=split["breaker"],
                linked=sum(1 for s in enabled if s["replacement"]["kind"] == "linked"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkout", required=True, help="a security_content checkout (data_sources/ is enough)")
    ap.add_argument("--out", required=True, help="directory to write one pack per data source into")
    ap.add_argument("--subset", help="comma-separated substrings of the file name or sourcetype to keep")
    ap.add_argument("--report", help="write the per-data-source outcome as JSON here")
    args = ap.parse_args(argv)
    import yaml  # PyYAML, only needed here

    os.makedirs(args.out, exist_ok=True)
    subset = [s.strip().lower() for s in (args.subset or "").split(",") if s.strip()]
    results = []
    for path in sorted(glob.glob(os.path.join(args.checkout, "data_sources", "*.yml"))):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        hay = (os.path.basename(path) + " " + str(doc.get("sourcetype") or "")).lower()
        if subset and not any(s in hay for s in subset):
            continue
        try:
            results.append(build_one(path, doc, args.out))
        except pb.BuilderError as exc:
            results.append({"file": os.path.basename(path), "status": "rejected", "reason": str(exc)})
    counts = {}  # type: Dict[str, int]
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps(counts))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"upstream": UPSTREAM, "counts": counts, "results": results}, fh, indent=1)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
