#!/usr/bin/env python3
"""Copy packs from one Stoker instance to another over the operator API.

Uses only the public endpoints — ``GET /api/packs``, ``GET /api/packs/{id}/export``
and ``POST /api/packs/upload`` — so it needs nothing but an operator token at
each end. Three modes, to cover an air gap in two hops:

    # 1. instance -> instance (both reachable from here)
    tools/pack_sync.py --from https://a.example.com --from-token stk_a \\
                       --to   https://b.example.com --to-token   stk_b

    # 2. instance -> a directory of .tar.gz files (carry it across the gap)
    tools/pack_sync.py --from https://a.example.com --from-token stk_a --out ./packs-out

    # 3. that directory -> instance (on the far side)
    tools/pack_sync.py --in ./packs-out --to https://b.example.com --to-token stk_b

``--filter`` keeps packs whose name contains any of the given substrings, and
``--dry-run`` lists what would move. A pack whose name already exists on the
destination is skipped unless ``--replace`` is given (which uploads it under the
same name; Stoker then holds two rows with that name, so prefer deleting the old
one first). Rawreplay datasets are embedded by default, which is what makes a
replay pack work on an instance with no internet access; ``--no-dataset`` skips
that and keeps the transfer small.

Exit code is 1 when any pack failed, so this is safe to run from CI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 900.0


def _req(url, token, method="GET", data=None, headers=None, timeout=TIMEOUT_S):
    # type: (str, str, str, object, object, float) -> tuple
    req = urllib.request.Request(url, method=method, data=data)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def list_packs(base, token):
    # type: (str, str) -> list
    status, body, _ = _req(base.rstrip("/") + "/api/packs", token)
    if status != 200:
        raise SystemExit("listing packs on %s failed: HTTP %d %s"
                         % (base, status, body[:200].decode("utf-8", "replace")))
    return json.loads(body)


def export_pack(base, token, pack_id, include_dataset=True):
    # type: (str, str, int, bool) -> tuple
    url = "%s/api/packs/%d/export" % (base.rstrip("/"), pack_id)
    if not include_dataset:
        url += "?include_dataset=false"
    status, body, headers = _req(url, token)
    if status != 200:
        raise RuntimeError("export failed: HTTP %d %s"
                           % (status, body[:300].decode("utf-8", "replace")))
    return body, headers.get("X-Stoker-Dataset-Embedded") == "true"


def upload_pack(base, token, filename, data, name=None):
    # type: (str, str, str, bytes, object) -> dict
    boundary = "----stoker-pack-sync-boundary"
    parts = []  # type: list
    if name:
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="name"\r\n\r\n%s\r\n'
             % (boundary, name)).encode("utf-8"))
    parts.append(
        ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
         'Content-Type: application/gzip\r\n\r\n' % (boundary, filename)).encode("utf-8"))
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    status, resp, _ = _req(
        base.rstrip("/") + "/api/packs/upload", token, method="POST", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    if status != 201:
        raise RuntimeError("upload failed: HTTP %d %s"
                           % (status, resp[:300].decode("utf-8", "replace")))
    return json.loads(resp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", help="source Stoker base URL")
    ap.add_argument("--from-token", dest="src_token", default=os.environ.get("STOKER_FROM_TOKEN"))
    ap.add_argument("--to", dest="dst", help="destination Stoker base URL")
    ap.add_argument("--to-token", dest="dst_token", default=os.environ.get("STOKER_TO_TOKEN"))
    ap.add_argument("--out", help="write the exports to this directory instead of uploading")
    ap.add_argument("--in", dest="src_dir", help="upload the .tar.gz files in this directory")
    ap.add_argument("--filter", help="comma-separated substrings of the pack name to keep")
    ap.add_argument("--no-dataset", action="store_true",
                    help="do not embed rawreplay datasets (smaller, needs network on the far side)")
    ap.add_argument("--replace", action="store_true",
                    help="upload even when the destination already has that pack name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.src and not args.src_dir:
        ap.error("give --from <url> or --in <dir>")
    if not args.dst and not args.out:
        ap.error("give --to <url> or --out <dir>")

    keep = [s.strip().lower() for s in (args.filter or "").split(",") if s.strip()]
    existing = set()
    if args.dst and not args.replace:
        existing = {p["name"] for p in list_packs(args.dst, args.dst_token)}

    # Source: either an instance's packs, or a directory of archives.
    if args.src_dir:
        items = [(None, os.path.splitext(os.path.splitext(f)[0])[0], f)
                 for f in sorted(os.listdir(args.src_dir))
                 if f.endswith((".tar.gz", ".tgz"))]
    else:
        items = [(p["id"], p["name"], None) for p in list_packs(args.src, args.src_token)]
    items = [it for it in items if not keep or any(s in it[1].lower() for s in keep)]
    if not items:
        print("nothing to copy", file=sys.stderr)
        return 1

    if args.out:
        os.makedirs(args.out, exist_ok=True)
    ok, skipped, failed = 0, 0, 0
    for pack_id, name, fname in items:
        if name in existing:
            print("skip  %-44s already on the destination" % name[:44])
            skipped += 1
            continue
        if args.dry_run:
            print("would %-44s %s" % (name[:44], "export" if pack_id else "upload"))
            continue
        try:
            if fname:
                with open(os.path.join(args.src_dir, fname), "rb") as fh:
                    data, embedded = fh.read(), None
                filename = fname
            else:
                data, embedded = export_pack(args.src, args.src_token, pack_id,
                                             include_dataset=not args.no_dataset)
                filename = "%s.tar.gz" % name
            if args.out:
                with open(os.path.join(args.out, filename), "wb") as fh:
                    fh.write(data)
                where = os.path.join(args.out, filename)
            else:
                pack = upload_pack(args.dst, args.dst_token, filename, data, name=name)
                where = "%s id=%s lint=%s" % (args.dst, pack["id"], pack["lint_status"])
                if pack["lint_status"] != "ok":
                    print("WARN  %-44s imported but FAILED LINT: %s"
                          % (name[:44], (pack.get("lint_errors_json") or [""])[0]))
        except (RuntimeError, OSError) as exc:
            print("FAIL  %-44s %s" % (name[:44], exc))
            failed += 1
            continue
        ds = "" if embedded is None else (" +dataset" if embedded else "")
        print("ok    %-44s %8d B%s -> %s" % (name[:44], len(data), ds, where))
        ok += 1
    print("\n%d copied, %d skipped, %d failed" % (ok, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
