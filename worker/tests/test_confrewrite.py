import textwrap

import pytest

from stoker_agent.confrewrite import (ConfRewriteError, largest_remainder,
                                      load_conf, make_parser, rewrite,
                                      rewrite_file, sample_sections,
                                      write_conf)

BASE_CONF = textwrap.dedent("""\
    [global]
    outputMode = httpevent
    httpeventServers = {"servers": [{"protocol": "https"}]}
    httpeventMaxPayloadSize = 10000
    index = main
    threading = process

    [sample1.csv]
    count = 10
    interval = 10
    sourcetype = access_combined
    source = /var/log/access.log
    host = web-1
    hourOfDayRate = {"0": 0.3, "1": 0.2}
    dayOfWeekRate = {"0": 0.9}
    randomizeCount = 0.2
    token.0.token = \\d{4}
    token.0.replacementType = timestamp

    [sample2.csv]
    count = 30
    interval = 10
    splunkHost = 192.168.0.222
    splunkPort = 8089

    [replay.csv]
    mode = replay
    sampletype = csv
    timeMultiple = 2
    outputMode = httpevent
    """)


def write_base_conf(tmp_path, text=BASE_CONF):
    path = tmp_path / "eventgen.conf"
    path.write_text(text)
    return str(path)


def rewritten(tmp_path, rate_mode, share, overdrive=1.15, slot=0,
              total_workers=1, text=BASE_CONF):
    src = write_base_conf(tmp_path, text)
    dst = str(tmp_path / "rewritten.conf")
    rewrite_file(src, dst, rate_mode, share, overdrive, "/bundle/samples",
                 slot=slot, total_workers=total_workers)
    return load_conf(dst)


class TestLargestRemainder:
    def test_sums_exactly(self):
        assert sum(largest_remainder(115, [1.0, 3.0])) == 115
        assert sum(largest_remainder(10, [1, 1, 1])) == 10
        assert sum(largest_remainder(7, [0.2, 0.3, 0.5])) == 7

    def test_proportions(self):
        assert largest_remainder(115, [1.0, 3.0]) == [29, 86]
        assert largest_remainder(10, [1, 1, 1]) == [4, 3, 3]

    def test_zero_weights_fall_back_to_equal(self):
        assert largest_remainder(9, [0.0, 0.0, 0.0]) == [3, 3, 3]

    def test_zero_total(self):
        assert largest_remainder(0, [1.0, 2.0]) == [0, 0]

    def test_empty(self):
        assert largest_remainder(5, []) == []

    def test_negative_total_rejected(self):
        with pytest.raises(ValueError):
            largest_remainder(-1, [1.0])


class TestStripAndStamp:
    def test_output_keys_stripped_everywhere(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        for section in conf.sections():
            for key in conf.options(section):
                assert not key.startswith("httpevent"), (section, key)
                assert key not in ("splunkHost", "splunkPort", "splunkMethod",
                                   "index", "sourcetype", "source", "host"), \
                    (section, key)

    def test_output_mode_is_stoker(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        for section in conf.sections():
            assert conf.get(section, "outputMode") == "stoker"

    def test_sample_dir_stamped(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        for section in conf.sections():
            assert conf.get(section, "sampleDir") == "/bundle/samples"

    def test_non_output_keys_survive(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        assert conf.get("global", "threading") == "process"
        assert conf.get("sample1.csv", "token.0.token") == "\\d{4}"
        # optionxform=str keeps key case
        assert conf.get("sample1.csv", "token.0.replacementType") == "timestamp"


class TestEpsMode:
    def test_counts_sum_to_overdriven_share(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100, overdrive=1.15)
        total = sum(conf.getint(s, "count")
                    for s in ("sample1.csv", "sample2.csv"))
        assert total == 115

    def test_apportioned_by_declared_estimates(self, tmp_path):
        # declared estimates 1 eps and 3 eps -> 25 % / 75 % of 115
        conf = rewritten(tmp_path, "eps", 100, overdrive=1.15)
        assert conf.getint("sample1.csv", "count") == 29
        assert conf.getint("sample2.csv", "count") == 86

    def test_interval_forced_to_one(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        assert conf.get("sample1.csv", "interval") == "1"
        assert conf.get("sample2.csv", "interval") == "1"

    def test_randomize_count_stripped(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        assert not conf.has_option("sample1.csv", "randomizeCount")

    def test_rate_maps_stripped_in_eps(self, tmp_path):
        # eps is a flat instantaneous rate: shaping maps would make the engine
        # under-produce during low-rate hours and starve the flat token
        # bucket, so they are removed (contract rule 5).
        conf = rewritten(tmp_path, "eps", 100)
        assert not conf.has_option("sample1.csv", "hourOfDayRate")
        assert not conf.has_option("sample1.csv", "dayOfWeekRate")

    def test_replay_untouched(self, tmp_path):
        conf = rewritten(tmp_path, "eps", 100)
        assert conf.get("replay.csv", "mode") == "replay"
        assert conf.get("replay.csv", "timeMultiple") == "2"
        assert not conf.has_option("replay.csv", "count")
        assert not conf.has_option("replay.csv", "interval")
        # output side still normalised
        assert conf.get("replay.csv", "outputMode") == "stoker"

    def test_count_floor_of_one(self, tmp_path):
        text = "[a.csv]\ncount = 1\ninterval = 1\n[b.csv]\ncount = 1000\ninterval = 1\n"
        conf = rewritten(tmp_path, "eps", 2, overdrive=1.0, text=text)
        assert conf.getint("a.csv", "count") >= 1
        assert conf.getint("b.csv", "count") >= 1

    def test_equal_split_when_undeclared(self, tmp_path):
        text = "[a.csv]\nfoo = 1\n[b.csv]\nbar = 2\n"
        conf = rewritten(tmp_path, "eps", 100, overdrive=1.0, text=text)
        assert conf.getint("a.csv", "count") == 50
        assert conf.getint("b.csv", "count") == 50

    def test_requires_share(self, tmp_path):
        src = write_base_conf(tmp_path)
        with pytest.raises(ConfRewriteError):
            rewrite(load_conf(src), "eps", None, 1.15, "/s")


class TestPerDayGbMode:
    def test_all_declared_scaled_proportionally(self, tmp_path):
        text = ("[a.csv]\nperDayVolume = 2\n"
                "[b.csv]\nperDayVolume = 6\n")
        conf = rewritten(tmp_path, "per_day_gb", 4, overdrive=1.0, text=text)
        assert conf.getfloat("a.csv", "perDayVolume") == pytest.approx(1.0)
        assert conf.getfloat("b.csv", "perDayVolume") == pytest.approx(3.0)

    def test_sum_equals_overdriven_share(self, tmp_path):
        text = ("[a.csv]\nperDayVolume = 2\n"
                "[b.csv]\nperDayVolume = 6\n")
        conf = rewritten(tmp_path, "per_day_gb", 10, overdrive=1.15, text=text)
        total = sum(conf.getfloat(s, "perDayVolume") for s in ("a.csv", "b.csv"))
        assert total == pytest.approx(11.5, rel=1e-4)

    def test_undeclared_get_equal_split_remainder(self, tmp_path):
        text = ("[a.csv]\nperDayVolume = 2\n"
                "[b.csv]\nperDayVolume = 6\n"
                "[c.csv]\nfoo = 1\n")
        conf = rewritten(tmp_path, "per_day_gb", 9, overdrive=1.0, text=text)
        assert conf.getfloat("c.csv", "perDayVolume") == pytest.approx(3.0)
        assert conf.getfloat("a.csv", "perDayVolume") == pytest.approx(1.5)
        assert conf.getfloat("b.csv", "perDayVolume") == pytest.approx(4.5)
        total = sum(conf.getfloat(s, "perDayVolume")
                    for s in ("a.csv", "b.csv", "c.csv"))
        assert total == pytest.approx(9.0, rel=1e-4)

    def test_replay_gets_no_volume(self, tmp_path):
        conf = rewritten(tmp_path, "per_day_gb", 10)
        assert not conf.has_option("replay.csv", "perDayVolume")


class TestCountIntervalMode:
    def test_count_split_across_workers(self, tmp_path):
        text = "[a.csv]\ncount = 10\ninterval = 30\nend = 1\n"
        for slot, expected in ((0, 3), (1, 3), (2, 2), (3, 2)):
            conf = rewritten(tmp_path, "count_interval", None, slot=slot,
                             total_workers=4, text=text)
            assert conf.getint("a.csv", "count") == expected, slot

    def test_interval_untouched(self, tmp_path):
        text = "[a.csv]\ncount = 10\ninterval = 30\nend = 1\n"
        conf = rewritten(tmp_path, "count_interval", None, slot=0,
                         total_workers=4, text=text)
        assert conf.get("a.csv", "interval") == "30"
        assert conf.get("a.csv", "end") == "1"

    def test_single_worker_keeps_count(self, tmp_path):
        text = "[a.csv]\ncount = 10\ninterval = 30\n"
        conf = rewritten(tmp_path, "count_interval", None, text=text)
        assert conf.getint("a.csv", "count") == 10

    def test_bad_slot_rejected(self, tmp_path):
        src = write_base_conf(tmp_path)
        with pytest.raises(ConfRewriteError):
            rewrite(load_conf(src), "count_interval", None, 1.15, "/s",
                    slot=4, total_workers=4)


class TestParserContract:
    def test_case_preserved_and_equals_delimiter(self, tmp_path):
        path = tmp_path / "case.conf"
        path.write_text("[S1]\nperDayVolume = 5\nCamelKey = x\n")
        conf = load_conf(str(path))
        assert conf.get("S1", "perDayVolume") == "5"
        assert conf.get("S1", "CamelKey") == "x"

    def test_colon_not_a_delimiter(self, tmp_path):
        path = tmp_path / "colon.conf"
        path.write_text("[S1]\ntoken.0.replacement = %Y-%m-%dT%H:%M:%S\n")
        conf = load_conf(str(path))
        assert conf.get("S1", "token.0.replacement") == "%Y-%m-%dT%H:%M:%S"

    def test_roundtrip_write(self, tmp_path):
        parser = make_parser()
        parser.add_section("a.csv")
        parser.set("a.csv", "count", "5")
        out = tmp_path / "out.conf"
        write_conf(parser, str(out))
        again = load_conf(str(out))
        assert again.get("a.csv", "count") == "5"

    def test_sample_sections_excludes_global_and_default(self, tmp_path):
        path = tmp_path / "g.conf"
        path.write_text("[global]\na = 1\n[default]\nb = 2\n[s.csv]\nc = 3\n")
        conf = load_conf(str(path))
        assert sample_sections(conf) == ["s.csv"]

    def test_unknown_mode_rejected(self, tmp_path):
        src = write_base_conf(tmp_path)
        with pytest.raises(ConfRewriteError):
            rewrite(load_conf(src), "warp", 1, 1.15, "/s")


class TestRateMaps:
    """Rule 5: eps strips shaping maps (flat rate); other modes preserve."""

    def test_per_day_gb_preserves_rate_maps(self, tmp_path):
        conf = rewritten(tmp_path, "per_day_gb", 1.0)
        assert conf.get("sample1.csv", "hourOfDayRate") == \
            '{"0": 0.3, "1": 0.2}'
        assert conf.get("sample1.csv", "dayOfWeekRate") == '{"0": 0.9}'

    def test_count_interval_preserves_rate_maps(self, tmp_path):
        conf = rewritten(tmp_path, "count_interval", None)
        assert conf.get("sample1.csv", "hourOfDayRate") == \
            '{"0": 0.3, "1": 0.2}'
        assert conf.get("sample1.csv", "dayOfWeekRate") == '{"0": 0.9}'

    def test_eps_strips_rate_maps_from_global(self, tmp_path):
        text = textwrap.dedent("""\
            [global]
            hourOfDayRate = {"0": 0.1}
            minuteOfHourRate = {"0": 0.5}

            [s.csv]
            count = 10
            interval = 10
            dayOfWeekRate = {"0": 0.9}
            """)
        conf = rewritten(tmp_path, "eps", 100, text=text)
        assert not conf.has_option("global", "hourOfDayRate")
        assert not conf.has_option("global", "minuteOfHourRate")
        assert not conf.has_option("s.csv", "dayOfWeekRate")

    def test_eps_leaves_replay_rate_maps_untouched(self, tmp_path):
        # rule 6: replay stanzas are never modified, even in eps mode
        text = textwrap.dedent("""\
            [r.csv]
            mode = replay
            timeMultiple = 2
            hourOfDayRate = {"0": 0.3}
            """)
        conf = rewritten(tmp_path, "eps", 100, text=text)
        assert conf.get("r.csv", "hourOfDayRate") == '{"0": 0.3}'
