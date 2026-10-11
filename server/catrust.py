"""The CA bundle that verifies a customer's HEC endpoint.

TLS verification used to be a boolean here, which gave an operator at a
customer site two options and no good one. A Splunk cluster behind a private
or internal CA fails verification against the public trust store, so the only
setting that worked was ``verify_tls = false`` - and that ships a HEC token
over a connection nobody checked, to a production indexer. The cert was fine;
we just had nowhere to put it.

``STOKER_CA_BUNDLE`` is that place: a path to a PEM file (or a directory of
them) mounted into the control plane, typically by whatever provisions the
deployment. It is used wherever verification is ON, so it composes with rather
than replaces the per-target ``verify_tls`` switch:

===================  ================  ==============================
``verify_tls``       ``CA_BUNDLE``     what the HTTP client is given
===================  ================  ==============================
true                 unset             ``True`` (public trust store)
true                 set               the bundle
false                either            ``False`` (no verification)
===================  ================  ==============================

**A fleet needs the bytes, not the path.** Workers are separate containers, so
a path that resolves in the control plane usually resolves to nothing in a
worker. The control plane therefore reads its bundle once and projects the PEM
text to workers as ``STOKER_CA_BUNDLE_PEM``; the worker writes it to a file of
its own and uses that. A CA certificate is public material, so it travels in
an environment variable without being a secret in the way a token is, and it
means the deployment only has to put the file in one place.

Validated at the point it is configured rather than per request: a bundle that
does not exist is a loud boot-time error, not a TLS failure in the middle of a
run that reads like the target refusing the connection.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Union

log = logging.getLogger("stoker.catrust")

#: Path to a PEM file, or a directory of them, used wherever TLS is verified.
CA_BUNDLE_ENV = "STOKER_CA_BUNDLE"

#: The same material as inline PEM text. What the control plane projects to a
#: worker, and what a worker prefers if both reach it.
CA_BUNDLE_PEM_ENV = "STOKER_CA_BUNDLE_PEM"


class CaBundleError(ValueError):
    """A configured CA bundle that cannot be used."""


def resolve_bundle(env=None):
    # type: (Optional[dict]) -> Optional[str]
    """The configured bundle path, validated, or None.

    Raises :class:`CaBundleError` when a path is set but unusable, because the
    alternative is a deployment that looks configured and silently verifies
    against the public trust store instead.
    """
    env = env if env is not None else os.environ
    path = (env.get(CA_BUNDLE_ENV) or "").strip()
    if not path:
        return None
    if not os.path.exists(path):
        raise CaBundleError(
            "%s points at %r, which does not exist. Mount the PEM file into the "
            "container at that path, or unset the variable to verify against the "
            "public trust store." % (CA_BUNDLE_ENV, path)
        )
    if os.path.isdir(path):
        return path
    if not os.path.isfile(path):
        raise CaBundleError(
            "%s points at %r, which is neither a file nor a directory"
            % (CA_BUNDLE_ENV, path)
        )
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        raise CaBundleError("%s points at %r, which cannot be read: %s"
                            % (CA_BUNDLE_ENV, path, exc))
    if b"-----BEGIN CERTIFICATE-----" not in head:
        # A DER file or the wrong file entirely. Saying so here beats a
        # handshake error that names neither the file nor the reason.
        raise CaBundleError(
            "%s points at %r, which does not look like PEM (no BEGIN CERTIFICATE "
            "in the first 4 KiB). Convert a DER certificate with: openssl x509 "
            "-inform der -in <file> -out bundle.pem" % (CA_BUNDLE_ENV, path)
        )
    return path


def bundle_pem(settings=None, env=None):
    # type: (Optional[Any], Optional[dict]) -> Optional[str]
    """The bundle's PEM text, for projecting to a worker. None when unset.

    Never raises: a run must not fail to launch because the bundle became
    unreadable after boot. A worker that receives no PEM verifies against the
    public trust store, which is the same behaviour as having no bundle at all.
    """
    path = getattr(settings, "ca_bundle", None) if settings is not None else None
    if not path:
        try:
            path = resolve_bundle(env)
        except CaBundleError as exc:
            log.warning("%s", exc)
            return None
    if not path:
        return None
    if os.path.isdir(path):
        # A directory is an OpenSSL hashed cert dir; there is no single file to
        # send. Projecting nothing is honest, and the message says what to do.
        log.warning(
            "%s is a directory, which cannot be projected to a fleet worker. Point "
            "it at a single concatenated PEM file so workers can verify too.",
            CA_BUNDLE_ENV)
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("could not read the CA bundle at %s to send to workers: %s",
                    path, exc)
        return None


def verify_arg(verify_tls, ca_bundle):
    # type: (bool, Optional[str]) -> Union[bool, str]
    """What to pass ``requests``' ``verify=``: it takes a path there directly."""
    if not verify_tls:
        return False
    return ca_bundle or True


def httpx_verify_arg(verify_tls, ca_bundle=None, env=None):
    # type: (bool, Optional[str], Optional[dict]) -> Any
    """What to pass ``httpx``'s ``verify=``.

    httpx 0.28 deprecated the bare path it used to accept, so a bundle has to
    become an ``SSLContext`` here. Falls back to plain verification if the
    context cannot be built, rather than failing the call: a target probe that
    errors tells the operator nothing about their certificate, and the
    configured path is validated loudly at boot anyway.
    """
    if not verify_tls:
        return False
    if ca_bundle is None:
        try:
            ca_bundle = resolve_bundle(env)
        except CaBundleError as exc:
            log.warning("%s", exc)
            ca_bundle = None
    if not ca_bundle:
        return True
    import ssl  # noqa: PLC0415 - only needed when a bundle is configured

    try:
        if os.path.isdir(ca_bundle):
            return ssl.create_default_context(capath=ca_bundle)
        return ssl.create_default_context(cafile=ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        log.warning("could not load the CA bundle at %s (%s); verifying against "
                    "the public trust store instead", ca_bundle, exc)
        return True


__all__ = [
    "CA_BUNDLE_ENV",
    "CA_BUNDLE_PEM_ENV",
    "CaBundleError",
    "bundle_pem",
    "httpx_verify_arg",
    "resolve_bundle",
    "verify_arg",
]
