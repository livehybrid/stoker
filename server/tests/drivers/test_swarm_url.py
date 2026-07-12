"""SwarmDriver Portainer URL normalisation.

Regression for the provisioning failure "swarm POST /services/create failed:
Request URL is missing an http:// or https://": a bare PORTAINER_HOST (no scheme,
e.g. 192.168.0.112) must be normalised to a full https URL so httpx accepts it,
matching infra/stacks/stoker/deploy.py's portainer_base.
"""
from __future__ import annotations

import pytest

from server.drivers.swarm import SwarmDriver, _portainer_base_url


@pytest.mark.parametrize(
    "host,expected",
    [
        ("192.168.0.112", "https://192.168.0.112:9443"),
        ("192.168.0.112/", "https://192.168.0.112:9443"),
        ("  192.168.0.112  ", "https://192.168.0.112:9443"),
        ("https://p.example:9443", "https://p.example:9443"),
        ("http://localhost:9000", "http://localhost:9000"),
        ("https://p.example:9443/", "https://p.example:9443"),
        ("", ""),
        (None, ""),
    ],
)
def test_portainer_base_url_normalisation(host, expected):
    assert _portainer_base_url(host) == expected


def test_driver_docker_base_is_scheme_full_from_bare_host():
    # A bare host must yield a full https docker base URL (httpx-acceptable).
    drv = SwarmDriver(host="192.168.0.112", token="x", endpoint=6)
    assert drv._docker_base() == "https://192.168.0.112:9443/api/endpoints/6/docker"


def test_driver_full_url_host_preserved():
    drv = SwarmDriver(host="https://portainer.internal:9443", token="x", endpoint=2)
    assert drv._docker_base() == "https://portainer.internal:9443/api/endpoints/2/docker"
