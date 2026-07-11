import pytest

from stoker_agent.config import load_config
from stoker_agent.slice import (SliceError, SpecSlice, format_iso8601,
                                parse_iso8601)

CLAIM_DOC = {
    "run_id": 812, "slot": 2, "total_workers": 4, "lease_id": "le_9f",
    "engine": "eventgen",
    "bundle": {"url": "https://ctl/api/agent/bundles/9c1f0a2b.tgz",
               "sha256": "9c1f0a2b"},
    "share": {"eps": 1543},
    "duration_s": 14400,
    "hec": {"url": "http://192.168.0.222:8088", "index": "loadtest",
            "sourcetype": None, "gzip": True, "ack": False},
    "overrides": {"host": "apigw-2"},
    "telemetry": {"interval_s": 5},
    "released": False,
}


def standalone_cfg(**extra):
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": "/packs/flatline",
        "STOKER_HEC_URL": "http://192.168.0.222:8088",
        "STOKER_HEC_TOKEN": "tok",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "100",
    }
    env.update(extra)
    return load_config(env)


class TestFromClaim:
    def test_contract_example(self):
        sl = SpecSlice.from_claim(dict(CLAIM_DOC))
        assert sl.run_id == 812
        assert sl.slot == 2
        assert sl.total_workers == 4
        assert sl.lease_id == "le_9f"
        assert sl.engine == "eventgen"
        assert sl.bundle_url.endswith("9c1f0a2b.tgz")
        assert sl.bundle_sha256 == "9c1f0a2b"
        assert sl.rate_mode == "eps"
        assert sl.rate_value == 1543.0
        assert sl.duration_s == 14400.0
        assert sl.hec_url == "http://192.168.0.222:8088"
        assert sl.hec_index == "loadtest"
        assert sl.hec_sourcetype is None
        assert sl.hec_gzip is True
        assert sl.hec_ack is False
        assert sl.overrides == {"host": "apigw-2"}
        assert sl.telemetry_interval_s == 5.0
        assert sl.released is False
        assert sl.effective_t0 is None

    def test_per_day_gb_share(self):
        doc = dict(CLAIM_DOC, share={"per_day_gb": 2.5})
        sl = SpecSlice.from_claim(doc)
        assert sl.rate_mode == "per_day_gb"
        assert sl.rate_value == 2.5

    def test_count_share_maps_to_count_interval(self):
        doc = dict(CLAIM_DOC, share={"count": 500})
        sl = SpecSlice.from_claim(doc)
        assert sl.rate_mode == "count_interval"
        assert sl.rate_value == 500.0

    def test_share_must_have_exactly_one_key(self):
        with pytest.raises(SliceError, match="exactly one key"):
            SpecSlice.from_claim(dict(CLAIM_DOC, share={}))
        with pytest.raises(SliceError, match="exactly one key"):
            SpecSlice.from_claim(
                dict(CLAIM_DOC, share={"eps": 1, "count": 2}))

    def test_unknown_share_key(self):
        with pytest.raises(SliceError, match="unknown share key"):
            SpecSlice.from_claim(dict(CLAIM_DOC, share={"warp": 9}))

    def test_non_positive_share_rejected(self):
        with pytest.raises(SliceError):
            SpecSlice.from_claim(dict(CLAIM_DOC, share={"eps": 0}))

    def test_missing_bundle_url(self):
        with pytest.raises(SliceError, match="bundle.url"):
            SpecSlice.from_claim(dict(CLAIM_DOC, bundle={}))

    def test_missing_hec_url(self):
        with pytest.raises(SliceError, match="hec.url"):
            SpecSlice.from_claim(dict(CLAIM_DOC, hec={"index": "x"}))

    def test_effective_t0_parsed(self):
        doc = dict(CLAIM_DOC, effective_t0="2026-07-11T12:00:00Z",
                   released=True)
        sl = SpecSlice.from_claim(doc)
        assert sl.released is True
        assert sl.effective_t0 == parse_iso8601("2026-07-11T12:00:00+00:00")

    def test_none_overrides_dropped(self):
        doc = dict(CLAIM_DOC, overrides={"host": "h1", "source": None})
        sl = SpecSlice.from_claim(doc)
        assert sl.overrides == {"host": "h1"}

    def test_unbounded_duration(self):
        doc = dict(CLAIM_DOC, duration_s=None)
        assert SpecSlice.from_claim(doc).duration_s is None


class TestFromStandalone:
    def test_synthesis(self):
        cfg = standalone_cfg(STOKER_SOURCETYPE="access",
                             STOKER_HOST_FIELD="apigw-2",
                             STOKER_SOURCE="/var/log/x",
                             STOKER_DURATION_S="120",
                             STOKER_SLOT="1", STOKER_TOTAL_WORKERS="2")
        sl = SpecSlice.from_standalone(cfg)
        assert sl.slot == 1
        assert sl.total_workers == 2
        assert sl.bundle_url == "/packs/flatline"
        assert sl.rate_mode == "eps"
        assert sl.rate_value == 100.0
        assert sl.duration_s == 120.0
        assert sl.hec_url == "http://192.168.0.222:8088"
        assert sl.hec_gzip is True
        assert sl.hec_ack is False
        # declared env metadata is run-declared: it wins over plugin values
        assert sl.overrides == {"index": "loadtest", "sourcetype": "access",
                                "host": "apigw-2", "source": "/var/log/x"}
        assert sl.released is False
        assert sl.effective_t0 is None

    def test_minimal_overrides_only_index(self):
        sl = SpecSlice.from_standalone(standalone_cfg())
        assert sl.overrides == {"index": "loadtest"}
        assert sl.hec_defaults()["index"] == "loadtest"
        assert sl.hec_defaults()["sourcetype"] is None


class TestIso8601:
    def test_z_suffix(self):
        assert parse_iso8601("2026-07-11T00:00:00Z") == \
            parse_iso8601("2026-07-11T00:00:00+00:00")

    def test_offset(self):
        # 01:00 at +01:00 is midnight UTC
        assert parse_iso8601("2026-07-11T01:00:00+01:00") == \
            parse_iso8601("2026-07-11T00:00:00Z")

    def test_naive_treated_as_utc(self):
        assert parse_iso8601("2026-07-11T00:00:00") == \
            parse_iso8601("2026-07-11T00:00:00Z")

    def test_roundtrip(self):
        epoch = 1752234567.0
        assert parse_iso8601(format_iso8601(epoch)) == epoch

    def test_invalid(self):
        with pytest.raises(SliceError):
            parse_iso8601("not-a-time")
