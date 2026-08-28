"""Pack upload (``POST /api/packs/upload``) + delete (``DELETE /api/packs/{id}``).

The upload endpoint extracts an UNTRUSTED archive supplied over HTTP, so most
of this file is the security suite for :mod:`server.packupload`: every archive
that tries to escape the extraction directory (``../`` members, absolute
paths), smuggle a link (symlink/hardlink — the same arbitrary-file-read
exfiltration ``bundles._iter_pack_files`` refuses at bundle time), create a
special node (device/fifo), or exhaust resources (member-count bombs, a lying
zip header, decompressed output past the caps) must be refused with an
operator-facing reason AND leave nothing behind on disk.

The happy paths prove the design constraint that an uploaded pack is an
ordinary local pack: both formats, both archive shapes (wrapped in a single
top-level directory, or rooted at the pack), content-based format detection
(a mis-named file still extracts), lint failures registering visibly rather
than 400ing, and the full flow into a spec + run on the fake fleet.

Caps are exercised by shrinking the ``pack_upload_*`` settings via
``dataclasses.replace`` on the conftest settings (the same pattern as
test_ceiling_config / test_builtin_packs), so the production defaults stay
untouched.
"""

from __future__ import annotations

import dataclasses
import io
import os
import struct
import tarfile
import zipfile
from typing import Any, List, Optional, Tuple

import pytest

from server import config as config_mod
from server import db as db_mod
from server import packupload

UPLOAD_PATH = "/api/packs/upload"


# --------------------------------------------------------------------------- #
# Fixtures + archive builders
# --------------------------------------------------------------------------- #

@pytest.fixture()
def upload_dir(settings, tmp_path):
    # type: (...) -> str
    """Point ``pack_upload_dir`` at a temp dir and return its path.

    Installed AFTER the conftest ``settings`` singleton (routes re-read
    ``get_settings()`` per request, so the patched value is what the endpoint
    uses); the conftest fixture's teardown still resets the singleton.
    """
    target = tmp_path / "uploads"
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), pack_upload_dir=str(target)))
    return str(target)


def _shrink_caps(**kwargs):
    # type: (...) -> None
    """Patch the upload caps on the installed settings singleton."""
    config_mod.set_settings(dataclasses.replace(
        config_mod.get_settings(), **kwargs))


def _tar_bytes(pack_dir, wrap=True, mode="w:gz", junk=False):
    # type: (str, bool, str, bool) -> bytes
    """Tar up ``pack_dir`` the way a customer would (optionally wrapped)."""
    base = os.path.basename(os.path.normpath(pack_dir))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        if junk:
            # macOS packaging noise a real customer archive often carries.
            info = tarfile.TarInfo("__MACOSX/._junk")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"junk"))
        for root, _dirs, files in os.walk(pack_dir):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pack_dir)
                arc = os.path.join(base, rel) if wrap else rel
                tar.add(full, arcname=arc)
    return buf.getvalue()


def _zip_bytes(pack_dir, wrap=True):
    # type: (str, bool) -> bytes
    base = os.path.basename(os.path.normpath(pack_dir))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(pack_dir):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pack_dir)
                arc = os.path.join(base, rel) if wrap else rel
                zf.write(full, arcname=arc)
    return buf.getvalue()


def _evil_tar(members):
    # type: (List[Tuple[tarfile.TarInfo, Optional[bytes]]]) -> bytes
    """A hand-built (possibly hostile) tar from raw TarInfo members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _file_member(name, data):
    # type: (str, bytes) -> Tuple[tarfile.TarInfo, bytes]
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _upload(client, data, filename="pack.tgz", name=None, description=None):
    # type: (Any, bytes, str, Optional[str], Optional[str]) -> Any
    form = {}
    if name is not None:
        form["name"] = name
    if description is not None:
        form["description"] = description
    return client.post(
        UPLOAD_PATH,
        files={"file": (filename, data, "application/octet-stream")},
        data=form)


def _entries(upload_dir):
    # type: (str) -> List[str]
    if not os.path.isdir(upload_dir):
        return []
    return sorted(os.listdir(upload_dir))


def _assert_rejected(resp, upload_dir, needle):
    # type: (Any, str, str) -> None
    """A rejection is a 400 with an operator-facing reason and a clean disk."""
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "pack_upload_rejected"
    assert needle in detail["detail"], detail
    # Nothing left behind: no pack dir, no staging remnants.
    assert _entries(upload_dir) == [], _entries(upload_dir)


# --------------------------------------------------------------------------- #
# Happy paths: both formats, both shapes, content detection, full flow.
# --------------------------------------------------------------------------- #

def test_upload_targz_wrapped_dir_registers_like_a_directory_pack(
        client, upload_dir, make_pack):
    data = _tar_bytes(make_pack("uploaded-flat"), wrap=True, junk=True)
    resp = _upload(client, data, name=None, description=None)
    assert resp.status_code == 201, resp.text
    pack = resp.json()
    # Name comes from pack.yaml (same as builtin seeding), lint ran clean.
    assert pack["name"] == "uploaded-flat"
    assert pack["lint_status"] == "ok" and pack["verified"] is True
    assert pack["stanza_count"] == 1
    assert pack["repo_id"] is None
    # The extracted directory persists under the upload dir and IS a pack root.
    assert pack["source_path"].startswith(upload_dir + os.sep)
    assert os.path.isfile(os.path.join(
        pack["source_path"], "default", "eventgen.conf"))
    # No staging remnants; exactly the one pack directory.
    assert _entries(upload_dir) == [os.path.basename(pack["source_path"])]
    # And it shows up in the ordinary pack list.
    listed = client.get("/api/packs").json()
    assert [p["id"] for p in listed] == [pack["id"]]


def test_upload_zip_unwrapped_and_plain_tar(client, upload_dir, make_pack):
    # zip, rooted at the pack itself (no wrapper directory).
    zresp = _upload(client, _zip_bytes(make_pack("zip-pack"), wrap=False),
                    filename="zip-pack.zip")
    assert zresp.status_code == 201, zresp.text
    assert zresp.json()["lint_status"] == "ok"
    # plain (uncompressed) tar, wrapped.
    tresp = _upload(client, _tar_bytes(make_pack("tar-pack"), mode="w"),
                    filename="tar-pack.tar")
    assert tresp.status_code == 201, tresp.text
    assert tresp.json()["name"] == "tar-pack"


def test_upload_detects_format_by_content_not_filename(
        client, upload_dir, make_pack):
    # A .tar.gz mis-named .zip must still extract (magic bytes win).
    data = _tar_bytes(make_pack("misnamed"))
    resp = _upload(client, data, filename="misnamed.zip")
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "misnamed"


def test_upload_operator_name_and_description_override(
        client, upload_dir, make_pack):
    resp = _upload(client, _tar_bytes(make_pack("renamed-src")),
                   name="customer-pack", description="from the customer")
    assert resp.status_code == 201, resp.text
    pack = resp.json()
    assert pack["name"] == "customer-pack"
    assert pack["description"] == "from the customer"
    # The operator's name also seeds the directory slug.
    assert os.path.basename(pack["source_path"]).startswith("customer-pack")


def test_upload_lint_failing_pack_registers_with_visible_errors(
        client, upload_dir, make_pack):
    # A structurally-valid pack whose conf points at a missing sample: the
    # upload must SUCCEED (201) with the lint errors in the response, so the
    # operator sees WHY the pack is bad instead of getting a bare 400.
    pack_dir = make_pack("broken-lint")
    os.remove(os.path.join(pack_dir, "samples", "broken-lint.sample"))
    resp = _upload(client, _tar_bytes(pack_dir))
    assert resp.status_code == 201, resp.text
    pack = resp.json()
    assert pack["lint_status"] == "error" and pack["verified"] is False
    assert any("sample file" in e for e in pack["lint_errors_json"])
    # The directory is kept: the operator can inspect / re-upload over it.
    assert os.path.isdir(pack["source_path"])


def test_uploaded_pack_flows_into_spec_and_run(client, upload_dir, make_pack,
                                               fake_driver):
    # The whole point of reusing the registration path: an uploaded pack must
    # drive a spec + run exactly like a directory pack.
    pack_id = _upload(client, _tar_bytes(make_pack("run-me"))).json()["id"]
    target = client.post("/api/targets", json={
        "name": "up-t", "hec_url": "http://127.0.0.1:18088", "token": "tok",
        "verify_tls": False})
    assert target.status_code == 201
    spec = client.post("/api/specs", json={
        "name": "up-spec", "pack_id": pack_id, "target_id": target.json()["id"],
        "rate_mode": "eps", "rate_value": 50, "workers": 1,
        "fleet": "fake-local"})
    assert spec.status_code == 201, spec.text
    run = client.post("/api/specs/%d/run" % spec.json()["id"], json={})
    assert run.status_code == 201, run.text


def test_upload_duplicate_names_get_distinct_directories(
        client, upload_dir, make_pack):
    data = _tar_bytes(make_pack("twice"))
    first = _upload(client, data).json()
    second = _upload(client, data).json()
    assert first["source_path"] != second["source_path"]
    assert os.path.isdir(first["source_path"])
    assert os.path.isdir(second["source_path"])


# --------------------------------------------------------------------------- #
# Rejections: not an archive / no recognisable pack.
# --------------------------------------------------------------------------- #

def test_upload_rejects_non_archive_bytes(client, upload_dir):
    resp = _upload(client, b"this is not an archive at all", filename="x.tgz")
    _assert_rejected(resp, upload_dir, "unrecognised archive")


def test_upload_rejects_empty_body(client, upload_dir):
    resp = _upload(client, b"")
    assert resp.status_code == 400
    assert resp.json()["detail"]["detail"] == "empty upload"
    assert _entries(upload_dir) == []


def test_upload_rejects_archive_with_no_pack_root(client, upload_dir, tmp_path):
    # A directory with none of the pack markers, wrapped as usual.
    junk_dir = tmp_path / "not-a-pack"
    junk_dir.mkdir()
    (junk_dir / "readme.txt").write_text("hello", encoding="utf-8")
    resp = _upload(client, _tar_bytes(str(junk_dir)))
    _assert_rejected(resp, upload_dir, "no pack found")


def test_upload_rejects_unreadable_pack_metadata(client, upload_dir, tmp_path):
    # stoker.json is a pack marker, so this passes find_pack_root, but the
    # reader raises on the bad JSON before the linter can collect it as a lint
    # error: the upload must reject cleanly (400 + no stranded directory),
    # never 500.
    bad = tmp_path / "bad-meta"
    bad.mkdir()
    (bad / "stoker.json").write_text("{not json", encoding="utf-8")
    resp = _upload(client, _tar_bytes(str(bad)))
    _assert_rejected(resp, upload_dir, "metadata unreadable")


def test_upload_rejects_two_top_level_directories(client, upload_dir, make_pack):
    # Two packs in one archive is ambiguous: which one did the operator mean?
    a, b = make_pack("multi-a"), make_pack("multi-b")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(a, arcname="multi-a")
        tar.add(b, arcname="multi-b")
    resp = _upload(client, buf.getvalue())
    _assert_rejected(resp, upload_dir, "no pack found")


# --------------------------------------------------------------------------- #
# Path traversal.
# --------------------------------------------------------------------------- #

def test_upload_rejects_tar_dotdot_traversal(client, upload_dir):
    data = _evil_tar([_file_member("../escape.conf", b"evil")])
    _assert_rejected(_upload(client, data), upload_dir, "escapes the extraction")
    # Nothing landed beside the upload dir either.
    assert not os.path.exists(os.path.join(
        os.path.dirname(upload_dir), "escape.conf"))


def test_upload_rejects_tar_nested_dotdot(client, upload_dir):
    data = _evil_tar([_file_member("pack/../../escape", b"evil")])
    _assert_rejected(_upload(client, data), upload_dir, "escapes the extraction")


def test_upload_rejects_tar_absolute_path(client, upload_dir):
    data = _evil_tar([_file_member("/tmp/absolute-evil", b"evil")])
    _assert_rejected(_upload(client, data), upload_dir, "absolute path")
    assert not os.path.exists("/tmp/absolute-evil")


def test_upload_rejects_windows_drive_and_unc_paths(client, upload_dir):
    drive = _evil_tar([_file_member("C:\\evil.conf", b"evil")])
    _assert_rejected(_upload(client, drive), upload_dir, "drive-letter")
    unc = _evil_tar([_file_member("\\\\server\\share\\evil", b"evil")])
    _assert_rejected(_upload(client, unc), upload_dir, "absolute path")


def test_upload_rejects_zip_dotdot_traversal(client, upload_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../zip-escape.conf", b"evil")
    _assert_rejected(_upload(client, buf.getvalue(), filename="e.zip"),
                     upload_dir, "escapes the extraction")


# --------------------------------------------------------------------------- #
# Links and special members: never created, always refused.
# --------------------------------------------------------------------------- #

def test_upload_rejects_tar_symlink_member(client, upload_dir):
    # The exfiltration primitive _iter_pack_files refuses at bundle time:
    # extraction must refuse it a layer earlier and never create the link.
    info = tarfile.TarInfo("samples/steal")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    _assert_rejected(_upload(client, _evil_tar([(info, None)])),
                     upload_dir, "symlink")


def test_upload_rejects_tar_hardlink_member(client, upload_dir):
    info = tarfile.TarInfo("samples/hard")
    info.type = tarfile.LNKTYPE
    info.linkname = "default/eventgen.conf"
    _assert_rejected(_upload(client, _evil_tar([(info, None)])),
                     upload_dir, "hardlink")


def test_upload_rejects_tar_device_and_fifo_members(client, upload_dir):
    dev = tarfile.TarInfo("dev-null")
    dev.type = tarfile.CHRTYPE
    dev.devmajor, dev.devminor = 1, 3
    _assert_rejected(_upload(client, _evil_tar([(dev, None)])),
                     upload_dir, "device")
    blk = tarfile.TarInfo("dev-blk")
    blk.type = tarfile.BLKTYPE
    blk.devmajor, blk.devminor = 8, 0
    _assert_rejected(_upload(client, _evil_tar([(blk, None)])),
                     upload_dir, "device")
    fifo = tarfile.TarInfo("a-fifo")
    fifo.type = tarfile.FIFOTYPE
    _assert_rejected(_upload(client, _evil_tar([(fifo, None)])),
                     upload_dir, "device")


def test_upload_rejects_zip_symlink_member(client, upload_dir):
    # Info-ZIP encodes a symlink as unix mode bits in external_attr; the link
    # target rides as the member data.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("samples/steal")
        info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
        zf.writestr(info, "/etc/passwd")
    _assert_rejected(_upload(client, buf.getvalue(), filename="s.zip"),
                     upload_dir, "symlink")


# --------------------------------------------------------------------------- #
# Extraction bombs: counts and byte caps enforced while streaming.
# --------------------------------------------------------------------------- #

def test_upload_rejects_member_count_bomb(client, upload_dir):
    _shrink_caps(pack_upload_max_members=5)
    data = _evil_tar([_file_member("f%d" % i, b"x") for i in range(10)])
    _assert_rejected(_upload(client, data), upload_dir, "more than 5 members")


def test_upload_rejects_oversized_member(client, upload_dir):
    _shrink_caps(pack_upload_max_member_bytes=1000)
    data = _evil_tar([_file_member("default/eventgen.conf", b"A" * 5000)])
    _assert_rejected(_upload(client, data), upload_dir, "per-file limit")


def test_upload_rejects_total_size_bomb(client, upload_dir):
    # Each member fits the per-file cap; the running total must still trip.
    _shrink_caps(pack_upload_max_member_bytes=1000,
                 pack_upload_max_total_bytes=2500)
    data = _evil_tar([_file_member("f%d" % i, b"B" * 900) for i in range(4)])
    _assert_rejected(_upload(client, data), upload_dir, "total uncompressed")


def test_upload_zip_cap_binds_on_decompressed_output_not_archive_size(
        client, upload_dir):
    # 4 MiB of zeros deflates to a few KiB: the archive sails under the body
    # cap, so only a cap enforced on the DECOMPRESSED stream can catch it.
    _shrink_caps(pack_upload_max_total_bytes=100_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("default/eventgen.conf", b"\x00" * (4 * 1024 * 1024))
    data = buf.getvalue()
    assert len(data) < 100_000  # the compressed archive itself is tiny
    _assert_rejected(_upload(client, data, filename="bomb.zip"),
                     upload_dir, "total uncompressed")


def test_upload_zip_with_lying_size_header_is_rejected_cleanly(
        client, upload_dir):
    # A zip whose headers under-declare the uncompressed size (the classic way
    # to defeat declared-size checks). Whatever the stdlib does with the
    # inconsistency, the outcome must be an operator-facing 400 and a clean
    # disk — never an unbounded write and never a 500.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("default/eventgen.conf", b"A" * (2 * 1024 * 1024))
    data = bytearray(buf.getvalue())
    lh = data.find(b"PK\x03\x04")
    cd = data.find(b"PK\x01\x02")
    struct.pack_into("<I", data, lh + 22, 10)  # local header file_size
    struct.pack_into("<I", data, cd + 24, 10)  # central directory file_size
    resp = _upload(client, bytes(data), filename="liar.zip")
    _assert_rejected(resp, upload_dir, "")  # 400 + clean disk; reason varies


def test_upload_body_cap_yields_413(client, upload_dir, make_pack):
    # A tiny cap the (compressed) archive is guaranteed to exceed.
    _shrink_caps(pack_upload_max_archive_bytes=100)
    resp = _upload(client, _tar_bytes(make_pack("too-big")))
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"]["error"] == "pack_upload_too_large"
    assert _entries(upload_dir) == []


# --------------------------------------------------------------------------- #
# The pure helpers.
# --------------------------------------------------------------------------- #

def test_detect_format_by_magic_bytes():
    assert packupload.detect_format(b"PK\x03\x04rest") == "zip"
    assert packupload.detect_format(b"\x1f\x8b\x08rest") == "tar"
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo("a")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    assert packupload.detect_format(tar_buf.getvalue()) == "tar"
    assert packupload.detect_format(b"not an archive") is None
    assert packupload.detect_format(b"") is None


# --------------------------------------------------------------------------- #
# Delete.
# --------------------------------------------------------------------------- #

def test_delete_uploaded_pack_removes_row_and_directory(
        client, upload_dir, make_pack):
    pack = _upload(client, _tar_bytes(make_pack("delete-me"))).json()
    assert os.path.isdir(pack["source_path"])
    resp = client.delete("/api/packs/%d" % pack["id"])
    assert resp.status_code == 204, resp.text
    assert not os.path.exists(pack["source_path"])
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 404


def test_delete_refused_while_a_spec_references_the_pack(
        client, upload_dir, make_pack):
    pack = _upload(client, _tar_bytes(make_pack("spec-held"))).json()
    target = client.post("/api/targets", json={
        "name": "del-t", "hec_url": "http://127.0.0.1:18088", "token": "tok"})
    spec = client.post("/api/specs", json={
        "name": "held-spec", "pack_id": pack["id"],
        "target_id": target.json()["id"], "rate_mode": "eps", "rate_value": 10,
        "workers": 1, "fleet": "fake-local"})
    assert spec.status_code == 201, spec.text
    resp = client.delete("/api/packs/%d" % pack["id"])
    assert resp.status_code == 409, resp.text
    assert "spec" in resp.json()["detail"]
    # The row AND the directory survive the refusal.
    assert client.get("/api/packs/%d" % pack["id"]).status_code == 200
    assert os.path.isdir(pack["source_path"])


def test_delete_refused_for_a_repo_indexed_pack(client, db_session, make_pack):
    from server.models import Pack, Repo

    repo = Repo(url="https://example.com/packs.git", auth_kind="none",
                default_ref="main")
    db_session.add(repo)
    db_session.flush()
    pack = Pack(name="from-repo", source_path=make_pack("from-repo"),
                repo_id=repo.id, lint_status="ok", verified=True)
    db_session.add(pack)
    db_session.commit()
    resp = client.delete("/api/packs/%d" % pack.id)
    assert resp.status_code == 409, resp.text
    assert "repo" in resp.json()["detail"]


def test_delete_local_pack_never_touches_a_non_uploaded_directory(
        client, upload_dir, make_pack):
    # A pack registered from an arbitrary directory (POST /api/packs): the row
    # goes, but its directory is NOT ours to delete.
    pack_dir = make_pack("keep-my-dir")
    created = client.post("/api/packs", json={
        "name": "keep-my-dir", "source_path": pack_dir})
    assert created.status_code == 201
    resp = client.delete("/api/packs/%d" % created.json()["id"])
    assert resp.status_code == 204, resp.text
    assert os.path.isdir(pack_dir)


def test_delete_unknown_pack_404(client, upload_dir):
    assert client.delete("/api/packs/999").status_code == 404


# --------------------------------------------------------------------------- #
# Auth: upload writes to the control-plane disk, so a viewer must be refused.
# --------------------------------------------------------------------------- #

def test_upload_and_delete_require_operator_role(settings, fake_driver, tmp_path):
    from fastapi.testclient import TestClient

    from server.app import create_app

    auth_settings = dataclasses.replace(
        settings,
        admin_user="root", admin_password="rootpw12345",
        pack_upload_dir=str(tmp_path / "uploads"))
    config_mod.set_settings(auth_settings)
    db_mod.configure(auth_settings.database_url)
    db_mod.create_all()

    with TestClient(create_app()) as admin_c:
        assert admin_c.post("/api/auth/login", json={
            "username": "root", "password": "rootpw12345"}).status_code == 200
        assert admin_c.post("/api/users", json={
            "username": "viewy", "password": "viewerpw12345",
            "role": "viewer"}).status_code == 201

    with TestClient(create_app()) as viewer_c:
        assert viewer_c.post("/api/auth/login", json={
            "username": "viewy", "password": "viewerpw12345"}).status_code == 200
        resp = viewer_c.post(UPLOAD_PATH, files={
            "file": ("p.tgz", b"anything", "application/octet-stream")})
        assert resp.status_code == 403, resp.text
        assert viewer_c.delete("/api/packs/1").status_code == 403
        # Nothing was written for the refused caller.
        assert not os.path.exists(str(tmp_path / "uploads"))
