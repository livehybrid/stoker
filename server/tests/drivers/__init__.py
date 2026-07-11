"""Driver test package: the shared ExecutionDriver conformance suite.

``test_conformance.py`` parametrises the six-method conformance walk over every
driver that can run in this environment (always the in-process FakeDriver;
SwarmDriver only when ``STOKER_TEST_PORTAINER=1`` points at a live Portainer),
and unit-tests the SwarmDriver's Portainer request shapes against a mocked
``httpx`` transport so it is covered without a swarm.
"""
