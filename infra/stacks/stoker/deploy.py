#!/usr/bin/env python3
"""Deploy (or update) the Stoker control-plane swarm stack via the Portainer API.

Creates a swarm stack from stack.yml, injecting env from this dir's .env and
ensuring the ``stoker_master_key`` swarm secret exists (created once from a
generated Fernet key persisted in .env so encrypted data survives redeploys).
Idempotent: an existing stack is updated in place.

Config: reads this dir's ``.env`` (copy .env.example), falling back to process
env. Required: PORTAINER_HOST, PORTAINER_TOKEN, STOKER_DB_PASSWORD. Optional:
STOKER_PUBLIC_BASE_URL, STOKER_WORKER_IMAGE, DOGFOOD_HEC_URL, DOGFOOD_HEC_TOKEN.

Usage:
  python deploy.py            # create or update the stack
  python deploy.py --dry-run  # show what would happen, change nothing
  python deploy.py --status   # show the current stack + services
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

HERE = Path(__file__).resolve().parent
ENDPOINT_ID = int(os.environ.get("PORTAINER_ENDPOINT", "6"))
SWARM_ID = os.environ.get("STOKER_SWARM_ID", "9xpdzr38fl4gfswthfvz8pwg0")
STACK_NAME = "stoker"
SECRET_NAME = "stoker_master_key"
ENV_KEYS = [
    "STOKER_DB_PASSWORD", "STOKER_PUBLIC_BASE_URL", "STOKER_WORKER_IMAGE",
    "PORTAINER_HOST", "PORTAINER_TOKEN", "DOGFOOD_HEC_URL", "DOGFOOD_HEC_TOKEN",
    "STOKER_BASICAUTH",   # Traefik basic-auth users string for the UI/ops router
    # App-level auth (see .env.example). All optional; the stack.yml supplies
    # ${VAR:-default} fallbacks, so an unset var just uses its default.
    "STOKER_ADMIN_USER", "STOKER_ADMIN_PASSWORD",
    "STOKER_TRUSTED_PROXIES", "STOKER_AUTH_HEADER", "STOKER_PROXY_DEFAULT_ROLE",
    "STOKER_SESSION_TTL",
]
DEFAULTS = {
    "STOKER_WORKER_IMAGE": "ghcr.io/livehybrid/stoker-worker:latest",
    "STOKER_PUBLIC_BASE_URL": "",  # set to the LAN/host URL workers reach us at
    "DOGFOOD_HEC_URL": "",
    "DOGFOOD_HEC_TOKEN": "",
}


def load_env(path: Path) -> dict:
    d = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def portainer_base(host: str) -> str:
    return host.rstrip("/") if host.startswith("http") else f"https://{host}:9443"


def generate_master_key() -> str:
    try:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()
    except Exception:  # cryptography not installed here: fall back to urandom
        return base64.urlsafe_b64encode(os.urandom(32)).decode()


def docker_api(base: str, H: dict, method: str, path: str, **kw):
    url = f"{base}/api/endpoints/{ENDPOINT_ID}/docker{path}"
    return requests.request(method, url, headers=H, verify=False, timeout=60, **kw)


def ensure_master_key_secret(base: str, H: dict, key_value: str, dry: bool) -> bool:
    """Create the stoker_master_key swarm secret if it does not already exist.

    Swarm secrets are immutable: if one exists we leave it (rotating means a new
    secret name + stack update, out of scope here). Returns True on success.
    """
    r = docker_api(base, H, "GET", "/secrets")
    if r.status_code != 200:
        print(f"ERROR listing secrets: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    for s in r.json():
        if s.get("Spec", {}).get("Name") == SECRET_NAME:
            print(f"Secret '{SECRET_NAME}' already exists (id={s.get('ID', '')[:12]}) — reusing.")
            return True
    if dry:
        print(f"[dry-run] would create swarm secret '{SECRET_NAME}'")
        return True
    body = {"Name": SECRET_NAME,
            "Data": base64.b64encode(key_value.encode()).decode(),
            "Labels": {"app": "stoker"}}
    r = docker_api(base, H, "POST", "/secrets/create", json=body)
    if r.status_code in (200, 201):
        print(f"Created swarm secret '{SECRET_NAME}'.")
        return True
    print(f"ERROR creating secret: HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
    return False


def find_stack(base: str, H: dict):
    r = requests.get(f"{base}/api/stacks", headers=H, verify=False, timeout=30)
    r.raise_for_status()
    return next((s for s in r.json() if s.get("Name") == STACK_NAME), None)


def show_status(base: str, H: dict) -> int:
    stack = find_stack(base, H)
    if not stack:
        print(f"Stack '{STACK_NAME}' not deployed.")
        return 0
    print(f"Stack '{STACK_NAME}' id={stack['Id']} status={stack.get('Status')}")
    r = docker_api(base, H, "GET", "/services")
    if r.status_code == 200:
        for svc in r.json():
            name = svc.get("Spec", {}).get("Name", "")
            if name.startswith("stoker"):
                mode = svc.get("Spec", {}).get("Mode", {}).get("Replicated", {})
                print(f"  service {name}: replicas={mode.get('Replicas', '?')}")
    return 0


def main() -> int:
    env = {**DEFAULTS, **load_env(HERE / ".env"), **{k: v for k, v in os.environ.items() if k in ENV_KEYS}}
    host, token = env.get("PORTAINER_HOST", ""), env.get("PORTAINER_TOKEN", "")
    if not host or not token:
        print("ERROR: PORTAINER_HOST / PORTAINER_TOKEN not set (see .env.example).", file=sys.stderr)
        return 1
    base = portainer_base(host)
    H = {"X-API-Key": token, "Content-Type": "application/json"}

    if "--status" in sys.argv:
        return show_status(base, H)

    dry = "--dry-run" in sys.argv

    if not env.get("STOKER_DB_PASSWORD"):
        print("ERROR: STOKER_DB_PASSWORD not set (see .env.example).", file=sys.stderr)
        return 1

    # Master key: reuse the one persisted in .env, else generate and persist it
    # so encrypted secrets survive a redeploy. Then ensure the swarm secret.
    stack_env_path = HERE / ".env"
    stack_env = load_env(stack_env_path)
    master_key = stack_env.get("STOKER_MASTER_KEY") or env.get("STOKER_MASTER_KEY")
    if not master_key:
        master_key = generate_master_key()
        if not dry:
            with open(stack_env_path, "a", encoding="utf-8") as fh:
                fh.write(f"\nSTOKER_MASTER_KEY={master_key}\n")
            print(f"Generated a new master key and appended it to {stack_env_path}.")
    if not ensure_master_key_secret(base, H, master_key, dry):
        return 1

    compose = (HERE / "stack.yml").read_text()
    env_pairs = [{"name": k, "value": env[k]} for k in ENV_KEYS if env.get(k)]
    print(f"Env injected: {[e['name'] for e in env_pairs]}")

    existing = find_stack(base, H)
    if dry:
        print(f"[dry-run] would {'UPDATE' if existing else 'CREATE'} stack '{STACK_NAME}' "
              f"on endpoint {ENDPOINT_ID} ({len(compose)} bytes).")
        return 0

    if existing:
        sid = existing["Id"]
        print(f"Stack exists (id={sid}) — updating in place…")
        body = {"stackFileContent": compose, "env": env_pairs, "prune": False, "pullImage": True}
        r = requests.put(f"{base}/api/stacks/{sid}?endpointId={ENDPOINT_ID}",
                         headers=H, json=body, verify=False, timeout=180)
    else:
        print(f"Creating swarm stack '{STACK_NAME}'…")
        body = {"name": STACK_NAME, "swarmID": SWARM_ID,
                "stackFileContent": compose, "env": env_pairs, "fromAppTemplate": False}
        r = requests.post(f"{base}/api/stacks/create/swarm/string?endpointId={ENDPOINT_ID}",
                          headers=H, json=body, verify=False, timeout=180)
        if r.status_code == 404:
            r = requests.post(f"{base}/api/stacks?type=1&method=string&endpointId={ENDPOINT_ID}",
                              headers=H, json=body, verify=False, timeout=180)

    print("HTTP", r.status_code)
    try:
        out = r.json()
    except Exception:
        print(r.text[:1000])
        return 0 if r.ok else 1
    if r.ok:
        print(f"OK — stack id={out.get('Id')} name={out.get('Name')}")
        print("Access on the LAN at http://<swarm-node-ip>:8091 (set STOKER_PUBLIC_BASE_URL "
              "to that URL so workers can reach the control plane).")
        return 0
    print("ERROR:", json.dumps(out)[:1000], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
