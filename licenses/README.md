# licenses/

Open-source licensing information for Stoker and everything it redistributes.

- **Stoker itself** is licensed under the **Apache License 2.0** — the
  authoritative text is the repository-root [`LICENSE`](../LICENSE), and
  attribution is in the root [`NOTICE`](../NOTICE).
- **[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)** inventories the
  third-party components redistributed in Stoker's build artifacts (the control-
  plane and worker container images, and the vendored eventgen engine) with
  their versions and SPDX licences.
- The `*.txt` files here are the full texts of each distinct licence used,
  named by SPDX identifier:

  | File | Used by (examples) |
  |------|--------------------|
  | `Apache-2.0.txt` | Stoker, vendored eventgen, kubernetes, bcrypt, requests |
  | `MIT.txt` | fastapi, pydantic, sqlalchemy, alembic, React, TanStack |
  | `BSD-3-Clause.txt` | uvicorn, starlette, httpx, itsdangerous, jinja2 |
  | `BSD-2-Clause.txt` | passlib |
  | `ISC.txt` | some transitive npm packages |
  | `PSF-2.0.txt` | typing-extensions |
  | `MPL-2.0.txt` | certifi (weak-copyleft; canonical-URL reference) |
  | `LGPL-3.0.txt` | psycopg (weak-copyleft; used unmodified as a library) |

The permissive texts (`MIT`, `BSD-*`, `ISC`) are the standard SPDX templates
with placeholder copyright lines; each dependency's own copyright holder is the
one named in the package. `Apache-2.0.txt`, `LGPL-3.0.txt` and `PSF-2.0.txt` are
verbatim copies of the canonical texts as shipped by the project / dependency.

This directory is curated, not auto-generated; see the end of
`THIRD-PARTY-LICENSES.md` for how to regenerate a full dependency inventory
after a version bump.
