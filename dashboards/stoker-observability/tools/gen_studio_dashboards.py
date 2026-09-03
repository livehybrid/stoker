#!/usr/bin/env python3
"""Generate the Dashboard Studio (view version=2) dashboards for the Stoker
observability app.

Studio dashboards are a strict JSON definition embedded in a Splunk view. Hand
-writing that JSON is error-prone, so we build it from Python dicts and
``json.dumps`` — the emitted JSON is well-formed by construction. Re-run to
regenerate the two views under ``app/default/data/ui/views/`` after editing the
panel/query model below.

    python3 tools/gen_studio_dashboards.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWS = os.path.join(HERE, "..", "app", "default", "data", "ui", "views")

METRICS = "index=$index_tok$ sourcetype=stoker:metrics"
JOBS = "index=$index_tok$ sourcetype=stoker:job"
TIME = {"earliest": "$global_time.earliest$", "latest": "$global_time.latest$"}


def ds(query, name):
    return {
        "type": "ds.search",
        "options": {"query": query, "queryParameters": dict(TIME)},
        "name": name,
    }


def single(ds_id, field, title, unit=None):
    opts = {
        "majorValue": "> primary | seriesByName('%s') | lastPoint()" % field,
        "sparklineDisplay": "off",
        "trendDisplay": "off",
    }
    if unit:
        opts["unit"] = unit
        opts["unitPosition"] = "after"
    return {"type": "splunk.singlevalue", "options": opts,
            "dataSources": {"primary": ds_id}, "title": title}


def line(ds_id, title, y_title):
    return {"type": "splunk.line",
            "options": {"legendDisplay": "bottom", "yAxisTitleText": y_title,
                        "nullValueDisplay": "connect"},
            "dataSources": {"primary": ds_id}, "title": title}


def area_stacked(ds_id, title, y_title):
    return {"type": "splunk.area",
            "options": {"legendDisplay": "right", "yAxisTitleText": y_title,
                        "stackMode": "stacked", "nullValueDisplay": "zero"},
            "dataSources": {"primary": ds_id}, "title": title}


def table(ds_id, title, drilldown_url=None):
    viz = {"type": "splunk.table", "options": {"count": 20},
           "dataSources": {"primary": ds_id}, "title": title}
    if drilldown_url:
        viz["options"]["drilldown"] = "row"
        viz["eventHandlers"] = [
            {"type": "drilldown.customUrl",
             "options": {"url": drilldown_url, "newTab": True}}]
    return viz


def block(item, x, y, w, h):
    return {"item": item, "type": "block", "position": {"x": x, "y": y, "w": w, "h": h}}


def dashboard(title, description, visualizations, data_sources, layout, inputs):
    return {
        "visualizations": visualizations,
        "dataSources": data_sources,
        "defaults": {"dataSources": {"ds.search": {"options": {"queryParameters": dict(TIME)}}}},
        "inputs": inputs,
        "layout": layout,
        "title": title,
        "description": description,
    }


def write_view(path, label, definition):
    meta = {"hideEdit": False, "hideOpenInSearch": False}
    xml = (
        '<dashboard version="2" theme="dark">\n'
        '  <label>%s</label>\n'
        '  <definition><![CDATA[\n%s\n]]></definition>\n'
        '  <meta type="hiddenElements"><![CDATA[\n%s\n]]></meta>\n'
        '</dashboard>\n'
    ) % (label, json.dumps(definition, indent=2), json.dumps(meta, indent=2))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    # Fail loudly if the embedded JSON is not well-formed.
    json.loads(json.dumps(definition))
    print("wrote", os.path.relpath(path))


# --------------------------------------------------------------------------- #
# Fleet overview
# --------------------------------------------------------------------------- #

def fleet():
    d = {
        "ds_active": ds(METRICS + "\n| stats latest(state) as state by run_id"
                        "\n| search state IN (provisioning, releasing, running, draining)"
                        "\n| stats count as active_runs", "Active runs"),
        "ds_eps": ds(METRICS + "\n| stats latest(eps) as eps by run_id"
                     "\n| stats sum(eps) as fleet_eps", "Fleet delivered eps"),
        "ds_events": ds(METRICS + "\n| stats max(events_total) as ev by run_id"
                        "\n| stats sum(ev) as events", "Events delivered"),
        "ds_errors": ds(METRICS + "\n| stats max(hec_4xx) as e4 max(hec_5xx) as e5 "
                        "max(hec_timeouts) as et by run_id"
                        "\n| eval errs = e4 + e5 + et"
                        "\n| stats sum(errs) as errors", "HEC errors"),
        "ds_eps_ts": ds(METRICS + '\n| timechart span=30s sum(eps) as "Delivered eps" '
                        'sum(target_eps) as "Target eps"', "Delivered vs target"),
        "ds_lag_ts": ds(METRICS + '\n| timechart span=30s max(lag_s_max) as "Max pacing lag (s)"',
                        "Pacing lag"),
        "ds_eps_by_run": ds(METRICS + "\n| timechart span=30s sum(eps) by run_id",
                            "Delivered eps by run"),
        "ds_mbps_by_run": ds(METRICS + "\n| eval mbps = bps / 1048576"
                             "\n| timechart span=30s sum(mbps) by run_id",
                             "MB/s by run"),
        "ds_hec_ts": ds(METRICS + '\n| timechart span=30s sum(hec_2xx) as "2xx" '
                        'sum(hec_4xx) as "4xx" sum(hec_5xx) as "5xx" '
                        'sum(hec_timeouts) as "timeouts" sum(retries) as "retries"',
                        "HEC outcomes"),
        "ds_runs": ds(METRICS + "\n| stats latest(state) as state latest(eps) as eps "
                      "latest(target_eps) as target_eps max(events_total) as events "
                      "latest(live_workers) as live latest(reporting_workers) as reporting "
                      "latest(lag_s_max) as lag_s by run_id spec_id"
                      "\n| sort - run_id", "Runs"),
        "ds_life": ds(JOBS + "\n| sort - _time"
                      "\n| table _time run_id from to end_reason degraded", "Lifecycle"),
    }
    drill = ("/app/stoker_observability/stoker_run_detail_studio"
             "?form.run_id_tok=$row.run_id.value$"
             "&form.global_time.earliest=$global_time.earliest$"
             "&form.global_time.latest=$global_time.latest$"
             "&form.index_tok=$index_tok$")
    v = {
        "viz_active": single("ds_active", "active_runs", "Active runs"),
        "viz_eps": single("ds_eps", "fleet_eps", "Fleet delivered eps", unit="eps"),
        "viz_events": single("ds_events", "events", "Events delivered (window)"),
        "viz_errors": single("ds_errors", "errors", "HEC errors (window)"),
        "viz_eps_ts": line("ds_eps_ts", "Delivered eps vs target — fleet total", "events / s"),
        "viz_lag_ts": line("ds_lag_ts", "Worst-worker pacing lag", "seconds behind"),
        "viz_eps_by_run": area_stacked("ds_eps_by_run",
                                       "Throughput — eps by run (stacked)", "events / s"),
        "viz_mbps_by_run": area_stacked("ds_mbps_by_run",
                                        "Throughput — MB/s by run (stacked)", "MB / s"),
        "viz_hec_ts": line("ds_hec_ts", "HEC outcomes — cumulative", "count"),
        "viz_runs": table("ds_runs", "Runs — click a row to drill in", drilldown_url=drill),
        "viz_life": table("ds_life", "Recent run lifecycle"),
    }
    layout = {
        "type": "absolute",
        "options": {"width": 1200, "height": 1500, "display": "auto"},
        "structure": [
            block("viz_active", 0, 0, 300, 150),
            block("viz_eps", 300, 0, 300, 150),
            block("viz_events", 600, 0, 300, 150),
            block("viz_errors", 900, 0, 300, 150),
            block("viz_eps_ts", 0, 160, 600, 300),
            block("viz_lag_ts", 610, 160, 590, 300),
            block("viz_eps_by_run", 0, 470, 600, 300),
            block("viz_mbps_by_run", 610, 470, 590, 300),
            block("viz_hec_ts", 0, 780, 1200, 280),
            block("viz_runs", 0, 1070, 600, 360),
            block("viz_life", 610, 1070, 590, 360),
        ],
        "globalInputs": ["input_time", "input_index"],
    }
    inputs = {
        "input_time": {"type": "input.timerange", "title": "Time range",
                       "options": {"token": "global_time", "defaultValue": "-60m,now"}},
        "input_index": {"type": "input.text", "title": "Index (dogfood target)",
                        "options": {"token": "index_tok", "defaultValue": "*"}},
    }
    return dashboard("Stoker — Fleet Observability",
                     "Every Stoker run at once, over the control plane's dogfood "
                     "telemetry. Click a run to drill in.", v, d, layout, inputs)


# --------------------------------------------------------------------------- #
# Per-run detail
# --------------------------------------------------------------------------- #

def detail():
    R = METRICS + " run_id=$run_id_tok$"
    RJ = JOBS + " run_id=$run_id_tok$"
    d = {
        "ds_state": ds(R + "\n| stats latest(state) as state", "State"),
        "ds_workers": ds(R + "\n| stats latest(reporting_workers) as reporting "
                         "latest(live_workers) as live"
                         '\n| eval workers = reporting . " / " . live', "Workers"),
        "ds_eps": ds(R + "\n| stats latest(eps) as eps", "Delivered eps"),
        "ds_events": ds(R + "\n| stats max(events_total) as events", "Events"),
        "ds_bytes": ds(R + "\n| stats max(bytes_total) as bytes"
                       "\n| eval mb = round(bytes/1024/1024, 1)", "Bytes"),
        "ds_eps_ts": ds(R + '\n| timechart span=30s avg(eps) as "Delivered eps" '
                        'avg(target_eps) as "Target eps"', "Delivered vs target"),
        "ds_mbps_ts": ds(R + "\n| eval mbps = bps / 1048576"
                         '\n| timechart span=30s avg(mbps) as "Delivered MB/s"',
                         "Delivered MB/s"),
        "ds_lag_ts": ds(R + '\n| timechart span=30s max(lag_s_max) as "Max pacing lag (s)"',
                        "Pacing lag"),
        "ds_hec_ts": ds(R + '\n| timechart span=30s max(hec_2xx) as "2xx" '
                        'max(hec_4xx) as "4xx" max(hec_5xx) as "5xx" '
                        'max(hec_timeouts) as "timeouts" max(retries) as "retries"',
                        "HEC outcomes"),
        "ds_life": ds(RJ + "\n| sort - _time"
                      "\n| table _time from to end_reason degraded", "Lifecycle"),
    }
    v = {
        "viz_state": single("ds_state", "state", "Current state"),
        "viz_workers": single("ds_workers", "workers", "Reporting / live workers"),
        "viz_eps": single("ds_eps", "eps", "Delivered eps", unit="eps"),
        "viz_events": single("ds_events", "events", "Events delivered"),
        "viz_bytes": single("ds_bytes", "mb", "MB delivered", unit="MB"),
        "viz_eps_ts": line("ds_eps_ts", "Delivered eps vs target", "events / s"),
        "viz_mbps_ts": line("ds_mbps_ts", "Delivered MB/s", "MB / s"),
        "viz_lag_ts": line("ds_lag_ts", "Pacing lag (max across workers)", "seconds behind"),
        "viz_hec_ts": line("ds_hec_ts", "HEC outcomes (cumulative)", "count"),
        "viz_life": table("ds_life", "Lifecycle (state transitions)"),
    }
    layout = {
        "type": "absolute",
        "options": {"width": 1200, "height": 1100, "display": "auto"},
        "structure": [
            block("viz_state", 0, 0, 240, 150),
            block("viz_workers", 240, 0, 240, 150),
            block("viz_eps", 480, 0, 240, 150),
            block("viz_events", 720, 0, 240, 150),
            block("viz_bytes", 960, 0, 240, 150),
            block("viz_eps_ts", 0, 160, 600, 300),
            block("viz_mbps_ts", 610, 160, 590, 300),
            block("viz_lag_ts", 0, 470, 600, 300),
            block("viz_hec_ts", 610, 470, 590, 300),
            block("viz_life", 0, 780, 1200, 280),
        ],
        "globalInputs": ["input_runid", "input_time", "input_index"],
    }
    inputs = {
        "input_runid": {"type": "input.text", "title": "Run id",
                        "options": {"token": "run_id_tok", "defaultValue": ""}},
        "input_time": {"type": "input.timerange", "title": "Time range",
                       "options": {"token": "global_time", "defaultValue": "-60m,now"}},
        "input_index": {"type": "input.text", "title": "Index (dogfood target)",
                        "options": {"token": "index_tok", "defaultValue": "*"}},
    }
    return dashboard("Stoker — Run Detail",
                     "One run by id, over dogfood telemetry: delivered vs target "
                     "eps, pacing lag, HEC outcomes and lifecycle.", v, d, layout, inputs)


def main():
    write_view(os.path.join(VIEWS, "stoker_fleet_observability_studio.xml"),
               "Stoker — Fleet Observability (Studio)", fleet())
    write_view(os.path.join(VIEWS, "stoker_run_detail_studio.xml"),
               "Stoker — Run Detail (Studio)", detail())


if __name__ == "__main__":
    main()
