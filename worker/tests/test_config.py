import pytest

from stoker_agent.config import Config, ConfigError, load_config


def standalone_env(**extra):
    env = {
        "STOKER_STANDALONE": "1",
        "STOKER_BUNDLE": "/packs/flatline",
        "STOKER_HEC_URL": "http://192.168.0.222:8088",
        "STOKER_HEC_TOKEN": "tok-abc",
        "STOKER_INDEX": "loadtest",
        "STOKER_RATE_MODE": "eps",
        "STOKER_RATE_VALUE": "100",
    }
    env.update(extra)
    return env


def managed_env(**extra):
    env = {
        "STOKER_RUN_ID": "812",
        "STOKER_CONTROL_URL": "https://stoker.mydomain.com",
        "STOKER_RUN_JWT": "jwt-opaque",
        "STOKER_TOTAL_WORKERS": "4",
        "STOKER_HEC_TOKEN": "tok-abc",
    }
    env.update(extra)
    return env


class TestStandalone:
    def test_minimal(self):
        cfg = load_config(standalone_env())
        assert cfg.standalone is True
        assert cfg.bundle == "/packs/flatline"
        assert cfg.hec_url == "http://192.168.0.222:8088"
        assert cfg.hec_token == "tok-abc"
        assert cfg.index == "loadtest"
        assert cfg.rate_mode == "eps"
        assert cfg.rate_value == 100.0
        assert cfg.duration_s is None
        assert cfg.slot == 0
        assert cfg.total_workers == 1
        assert cfg.engine == "eventgen"

    def test_tuning_defaults(self):
        cfg = load_config(standalone_env())
        assert cfg.output_socket == "/tmp/stoker-output.sock"
        assert cfg.heartbeat_s == 5.0
        assert cfg.overdrive == 1.15
        assert cfg.catchup_s == 5.0
        assert cfg.metrics_port == 9100
        assert cfg.deadman_s == 600.0
        assert cfg.hec_verify_tls is True

    def test_tuning_overrides(self):
        cfg = load_config(standalone_env(
            STOKER_OUTPUT_SOCKET="/tmp/x.sock",
            STOKER_HEARTBEAT_S="2",
            STOKER_OVERDRIVE="1.3",
            STOKER_CATCHUP_S="10",
            STOKER_METRICS_PORT="0",
            STOKER_DEADMAN_S="1800",
        ))
        assert cfg.output_socket == "/tmp/x.sock"
        assert cfg.heartbeat_s == 2.0
        assert cfg.overdrive == 1.3
        assert cfg.catchup_s == 10.0
        assert cfg.metrics_port == 0
        assert cfg.deadman_s == 1800.0

    def test_duration_and_slots(self):
        cfg = load_config(standalone_env(
            STOKER_DURATION_S="120", STOKER_SLOT="2",
            STOKER_TOTAL_WORKERS="4"))
        assert cfg.duration_s == 120.0
        assert cfg.slot == 2
        assert cfg.total_workers == 4

    def test_empty_duration_is_unbounded(self):
        cfg = load_config(standalone_env(STOKER_DURATION_S=""))
        assert cfg.duration_s is None

    def test_count_interval_ignores_rate_value(self):
        env = standalone_env(STOKER_RATE_MODE="count_interval")
        del env["STOKER_RATE_VALUE"]
        cfg = load_config(env)
        assert cfg.rate_mode == "count_interval"
        assert cfg.rate_value is None

    def test_index_required(self):
        env = standalone_env()
        del env["STOKER_INDEX"]
        with pytest.raises(ConfigError, match="STOKER_INDEX"):
            load_config(env)

    def test_rate_value_required_for_eps(self):
        env = standalone_env()
        del env["STOKER_RATE_VALUE"]
        with pytest.raises(ConfigError, match="STOKER_RATE_VALUE"):
            load_config(env)

    def test_bad_rate_mode(self):
        with pytest.raises(ConfigError, match="STOKER_RATE_MODE"):
            load_config(standalone_env(STOKER_RATE_MODE="warp"))

    def test_zero_rate_rejected(self):
        with pytest.raises(ConfigError, match="STOKER_RATE_VALUE"):
            load_config(standalone_env(STOKER_RATE_VALUE="0"))

    def test_slot_out_of_range(self):
        with pytest.raises(ConfigError, match="STOKER_SLOT"):
            load_config(standalone_env(STOKER_SLOT="1",
                                       STOKER_TOTAL_WORKERS="1"))

    def test_bad_engine(self):
        with pytest.raises(ConfigError, match="STOKER_ENGINE"):
            load_config(standalone_env(STOKER_ENGINE="chaos"))


class TestManaged:
    def test_minimal(self):
        cfg = load_config(managed_env())
        assert cfg.standalone is False
        assert cfg.run_id == "812"
        assert cfg.control_url == "https://stoker.mydomain.com"
        assert cfg.run_jwt == "jwt-opaque"
        assert cfg.total_workers == 4
        assert cfg.hint_slot is None
        assert cfg.holder  # defaults to hostname

    def test_hint_slot_and_holder(self):
        cfg = load_config(managed_env(STOKER_HINT_SLOT="2",
                                      STOKER_HOLDER="worker-2"))
        assert cfg.hint_slot == 2
        assert cfg.holder == "worker-2"

    def test_control_url_trailing_slash_stripped(self):
        cfg = load_config(managed_env(
            STOKER_CONTROL_URL="https://ctl.example.com/"))
        assert cfg.control_url == "https://ctl.example.com"

    def test_missing_run_id(self):
        env = managed_env()
        del env["STOKER_RUN_ID"]
        with pytest.raises(ConfigError, match="STOKER_RUN_ID"):
            load_config(env)

    def test_missing_jwt(self):
        env = managed_env()
        del env["STOKER_RUN_JWT"]
        with pytest.raises(ConfigError, match="STOKER_RUN_JWT"):
            load_config(env)

    def test_missing_hec_token(self):
        env = managed_env()
        del env["STOKER_HEC_TOKEN"]
        with pytest.raises(ConfigError, match="STOKER_HEC_TOKEN"):
            load_config(env)

    def test_non_http_control_url(self):
        with pytest.raises(ConfigError, match="STOKER_CONTROL_URL"):
            load_config(managed_env(STOKER_CONTROL_URL="ftp://x"))

    def test_bad_total_workers(self):
        with pytest.raises(ConfigError, match="STOKER_TOTAL_WORKERS"):
            load_config(managed_env(STOKER_TOTAL_WORKERS="zero"))


def test_config_is_frozen():
    cfg = load_config(standalone_env())
    with pytest.raises(Exception):
        cfg.hec_token = "other"
    assert isinstance(cfg, Config)
