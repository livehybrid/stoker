# Third-party licences

Stoker is licensed under the Apache License 2.0 (see the root `LICENSE` and
`NOTICE`). This file lists the third-party open-source components redistributed
in Stoker's build artifacts (the `ghcr.io/livehybrid/stoker` and
`stoker-worker` container images, and the vendored engine in-tree) together with
their licences. Full licence texts are in this directory, keyed by SPDX id.

Licences seen here: **Apache-2.0**, **MIT**, **BSD-3-Clause**, **BSD-2-Clause**,
**ISC**, **PSF-2.0**, **MPL-2.0** (`certifi`), **LGPL-3.0-only** (`psycopg`).
The two weak-copyleft ones are called out explicitly below.

Versions are those resolved at the time of writing (2026-07-12); regenerate the
inventory after a dependency bump (see the bottom of this file).

## Vendored source (in-tree)

| Component | Version | Licence | Location |
|-----------|---------|---------|----------|
| splunk_eventgen | 7.2.1 | Apache-2.0 | `worker/engines/eventgen/` — upstream text at `worker/engines/eventgen/LICENSE.upstream`, modifications in `worker/engines/eventgen/VENDOR.md` |

## Python — control plane (`server/requirements.txt` + transitive)

| Package | Version | SPDX licence |
|---------|---------|--------------|
| fastapi | 0.139.0 | MIT |
| starlette | 1.3.1 | BSD-3-Clause |
| uvicorn | 0.51.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| annotated-types | 0.7.0 | MIT |
| sqlalchemy | 2.0.51 | MIT |
| greenlet | 3.5.3 | MIT AND PSF-2.0 |
| alembic | 1.18.5 | MIT |
| mako | 1.3.12 | MIT |
| MarkupSafe | 2.1.5 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpcore / h11 | 0.16.x | MIT |
| anyio | 4.14.1 | MIT |
| idna | 3.18 | BSD-3-Clause |
| certifi | 2026.6.17 | **MPL-2.0** (see note) |
| pyjwt | 2.13.0 | MIT |
| cryptography | 49.0.0 | Apache-2.0 OR BSD-3-Clause |
| passlib | 1.7.4 | BSD-2-Clause |
| bcrypt | 4.0.1 | Apache-2.0 |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| **psycopg / psycopg-binary** | 3.3.4 | **LGPL-3.0-only** (see note) |
| kubernetes | 31.0.0 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.7.0 | MIT |
| google-auth | 2.55.2 | Apache-2.0 |
| websocket-client | 1.9.0 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| six | 1.17.0 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |

`pytest` / `pytest-timeout` (MIT) are present in the image via
`requirements.txt` but are test-only and not exercised at runtime.

## Python — worker (`worker/requirements.txt`, drives the vendored engine)

| Package | Licence |
|---------|---------|
| jinja2 | BSD-3-Clause |
| python-dateutil | Apache-2.0 AND BSD-3-Clause |

(The worker also uses the Python standard library and the same HEC/HTTP stack as
the control plane.)

## JavaScript — UI (`ui/package.json`, built into the control-plane image)

Direct dependencies:

| Package | Licence |
|---------|---------|
| react, react-dom | MIT |
| @tanstack/react-query | MIT |
| @tanstack/react-router, @tanstack/router-plugin | MIT |
| recharts | MIT |
| vite, @vitejs/plugin-react | MIT |
| tailwindcss, postcss, autoprefixer | MIT |
| typescript | Apache-2.0 |
| @types/react, @types/react-dom | MIT (DefinitelyTyped) |

The transitive npm dependency tree bundled by Vite is predominantly MIT, with a
smaller number of ISC and BSD (2/3-Clause) packages, all OSI-approved permissive
licences. Run `npx license-checker --summary` in `ui/` for the exhaustive tree.

## Notes on the weak-copyleft dependencies

- **`psycopg` / `psycopg-binary` — LGPL-3.0-only.** The Postgres driver is used
  unmodified as an ordinary, dynamically-imported library (installed via pip,
  not modified and not statically linked into Stoker's own code). Using an
  unmodified LGPL library from an Apache-2.0 work is permitted; the driver
  remains under LGPL-3.0 and its full text is in `licenses/LGPL-3.0.txt`. LGPL-3.0
  incorporates the GNU GPL-3.0 by reference (https://www.gnu.org/licenses/gpl-3.0).
- **`certifi` — MPL-2.0.** Redistributed unmodified; MPL-2.0 requires only that
  modifications to the MPL-covered files stay under MPL-2.0, and Stoker makes
  none. See `licenses/MPL-2.0.txt`.

## Regenerating this inventory

```bash
# Python (from the control-plane venv):
pip install pip-licenses && pip-licenses --format=markdown --with-urls
# JavaScript (from ui/):
npx license-checker --summary
```
Update this file and add any newly-seen SPDX licence text to `licenses/` when a
dependency is added or bumped.
