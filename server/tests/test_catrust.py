"""The CA bundle that lets a customer's private CA be verified rather than ignored.

The behaviour worth pinning is the truth table (``verify_tls`` x bundle) and
the two loud failures: a path that does not exist, and a file that is not PEM.
Both used to surface as a TLS handshake error naming neither the file nor the
reason, which at a customer site reads like their network rather than our
configuration.
"""
from __future__ import annotations

import ssl

import certifi
import pytest

from server import catrust
from server.config import ConfigError, Settings, load_settings


@pytest.fixture()
def bundle(tmp_path):
    """A real PEM, so the SSLContext path is genuinely exercised."""
    path = tmp_path / "customer-ca.pem"
    with open(certifi.where(), encoding="utf-8") as handle:
        path.write_text(handle.read(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #

def test_unset_means_the_public_trust_store():
    assert catrust.resolve_bundle({}) is None
    assert catrust.httpx_verify_arg(True, None) is True
    assert catrust.verify_arg(True, None) is True


def test_a_configured_bundle_resolves(bundle):
    assert catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(bundle)}) == str(bundle)


def test_a_directory_of_certificates_is_accepted(tmp_path, bundle):
    directory = tmp_path / "certs"
    directory.mkdir()
    assert catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(directory)}) == str(directory)


def test_a_missing_path_is_loud_not_silent():
    """The failure this whole setting exists to prevent.

    A deployment handed a certificate path that quietly verified against the
    public trust store instead would show up only as every target reading
    "down" for a reason nothing names.
    """
    with pytest.raises(catrust.CaBundleError) as caught:
        catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: "/no/such/ca.pem"})
    assert "does not exist" in str(caught.value)
    assert catrust.CA_BUNDLE_ENV in str(caught.value)


def test_a_file_that_is_not_pem_is_refused_with_the_fix(tmp_path):
    der = tmp_path / "ca.der"
    der.write_bytes(b"\x30\x82\x03\x0a definitely not pem")
    with pytest.raises(catrust.CaBundleError) as caught:
        catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(der)})
    message = str(caught.value)
    assert "does not look like PEM" in message
    # The message carries the command that fixes it, because the operator
    # hitting this is usually holding a .der from a Windows CA export.
    assert "openssl x509 -inform der" in message


# --------------------------------------------------------------------------- #
# The truth table
# --------------------------------------------------------------------------- #

def test_the_bundle_is_used_only_where_verification_is_on(bundle):
    """It composes with verify_tls rather than overriding it.

    A target explicitly set not to verify must stay not verifying: an operator
    who turned it off for a throwaway lab stack should not find that adding a
    bundle for a different target silently switched it back on.
    """
    assert catrust.httpx_verify_arg(False, str(bundle)) is False
    assert catrust.verify_arg(False, str(bundle)) is False


def test_httpx_gets_an_ssl_context_not_a_path(bundle):
    """httpx 0.28 deprecated the bare path its verify= used to take."""
    resolved = catrust.httpx_verify_arg(True, str(bundle))
    assert isinstance(resolved, ssl.SSLContext)


def test_requests_gets_the_path_directly(bundle):
    """The worker's HEC client is requests, which takes a path there."""
    assert catrust.verify_arg(True, str(bundle)) == str(bundle)


def test_an_unloadable_bundle_falls_back_rather_than_failing_the_call(tmp_path):
    """A probe that errors tells the operator nothing about their certificate.

    The path is validated loudly at boot, so by the time a request is made the
    useful thing is to still answer, having said why.
    """
    broken = tmp_path / "broken.pem"
    broken.write_text("-----BEGIN CERTIFICATE-----\nnot base64 at all\n", encoding="utf-8")
    assert catrust.httpx_verify_arg(True, str(broken)) is True


# --------------------------------------------------------------------------- #
# Reaching a fleet worker
# --------------------------------------------------------------------------- #

def test_the_pem_is_readable_for_projection(bundle):
    """A worker is its own container, so it needs the bytes, not the path."""
    pem = catrust.bundle_pem(None, {catrust.CA_BUNDLE_ENV: str(bundle)})
    assert pem and "-----BEGIN CERTIFICATE-----" in pem


def test_a_directory_cannot_be_projected_and_says_so(tmp_path, caplog):
    directory = tmp_path / "certs"
    directory.mkdir()
    with caplog.at_level("WARNING"):
        assert catrust.bundle_pem(None, {catrust.CA_BUNDLE_ENV: str(directory)}) is None
    assert "cannot be projected" in caplog.text


def test_projection_never_raises_after_boot(tmp_path, caplog):
    """A run must not fail to launch because the bundle became unreadable."""
    with caplog.at_level("WARNING"):
        assert catrust.bundle_pem(None, {catrust.CA_BUNDLE_ENV: "/gone/ca.pem"}) is None


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def test_settings_carry_the_bundle(bundle, monkeypatch):
    monkeypatch.setenv(catrust.CA_BUNDLE_ENV, str(bundle))
    assert load_settings().ca_bundle == str(bundle)


def test_a_bad_bundle_refuses_to_boot(monkeypatch):
    monkeypatch.setenv(catrust.CA_BUNDLE_ENV, "/no/such/ca.pem")
    with pytest.raises(ConfigError):
        load_settings()


def test_settings_default_to_no_bundle(monkeypatch):
    monkeypatch.delenv(catrust.CA_BUNDLE_ENV, raising=False)
    assert load_settings().ca_bundle is None
