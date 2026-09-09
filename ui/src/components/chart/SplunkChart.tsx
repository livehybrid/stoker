/*
 * The Splunk chart itself, in its own module so it can be loaded on demand.
 *
 * @splunk/visualizations is by far the largest thing this console depends on:
 * the charting bundle underneath it carries Highcharts and its own copy of
 * jQuery. Most pages have no chart on them (specs, packs, repos, targets,
 * users, and the sign-in screen), so this is split out and imported lazily by
 * LineChart. Pages that draw nothing never download it.
 */
import { useMemo } from "react";
import Line from "@splunk/visualizations/Line";
import { useSplunkTheme } from "@splunk/themes";

import type { ChartMarker, ChartProps, ChartSeries } from "./types";

/** Splunk wants ISO 8601 on `_time`; the app carries epoch milliseconds. */
const iso = (epochMs: number) => new Date(epochMs).toISOString();

/**
 * Turn the series into one Splunk dataSource.
 *
 * Series do not have to share x values: a run's target rate only exists once
 * leases are granted, so it starts later than the actual rate. The x column is
 * therefore the union of every series' x values, and a series with no value at
 * one of them gets a null, which `nullValueDisplay: 'gaps'` draws as a break
 * in the line rather than a straight line across it. A worker that stopped
 * reporting has to look like a hole, not like steady throughput.
 */
function toDataSource(series: ChartSeries[], xField: string, xIsTime: boolean) {
  const xs = new Set<number | string>();
  series.forEach((s) => s.points.forEach(([x]) => xs.add(x)));
  const values = Array.from(xs).sort((a, b) =>
    typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b)),
  );
  const index = new Map(values.map((v, i) => [v, i]));

  const columns: (string | null)[][] = [
    values.map((v) => (xIsTime ? iso(v as number) : String(v))),
  ];
  series.forEach((s) => {
    const column: (string | null)[] = new Array(values.length).fill(null);
    s.points.forEach(([x, y]) => {
      if (y !== null && y !== undefined && !Number.isNaN(y)) {
        column[index.get(x) as number] = String(y);
      }
    });
    columns.push(column);
  });

  return {
    // requestParams is declared required on Splunk's DataSource type. It
    // describes the paging of the search that produced the rows; there is no
    // search here, so it describes all of them.
    requestParams: { offset: 0, count: values.length },
    data: {
      fields: [{ name: xField }, ...series.map((s) => ({ name: s.label }))],
      columns,
    },
    meta: { totalCount: values.length },
  };
}

/*
 * Markers, as the chart's own annotations.
 *
 * annotationX/Label/Color take plain arrays. Splunk's examples feed them from
 * a second dataSource through the dynamic-options DSL ("> annotation|..."),
 * which is how a Studio dashboard binds a search to them; outside a dashboard
 * that form creates the annotation layer and leaves it empty, with no error
 * anywhere. Each becomes a dashed vertical rule in the marker's colour with
 * the label on hover.
 */
function toAnnotations(markers: ChartMarker[] | undefined, theme: Record<string, string>) {
  if (!markers || !markers.length) {
    return {};
  }
  const colour = (kind: ChartMarker["kind"]) =>
    kind === "warning"
      ? theme.warningColor
      : kind === "error"
        ? theme.errorColor
        : theme.contentColorMuted;
  return {
    annotationX: markers.map((m) => iso(m.at)),
    annotationLabel: markers.map((m) => m.label),
    annotationColor: markers.map((m) => colour(m.kind)),
  };
}

interface SplunkChartProps extends ChartProps {
  width: number;
  height: number;
}

export default function SplunkChart({
  series,
  markers,
  leftTitle,
  rightTitle,
  xTitle,
  xKind = "time",
  width,
  height,
}: SplunkChartProps) {
  const theme = useSplunkTheme() as unknown as Record<string, string>;
  const xIsTime = xKind === "time";
  const xField = xIsTime ? "_time" : xTitle || "x";

  const { dataSources, options } = useMemo(() => {
    const overlay = series.filter((s) => s.axis === "right").map((s) => s.label);
    const colours: Record<string, string> = {};
    const dashes: Record<string, string> = {};
    series.forEach((s) => {
      if (s.color) {
        colours[s.label] = s.color;
      }
      if (s.dashed) {
        dashes[s.label] = "shortDash";
      }
    });

    return {
      dataSources: { primary: toDataSource(series, xField, xIsTime) },
      options: {
        nullValueDisplay: "gaps",
        legendDisplay: "bottom",
        markerDisplay: "off",
        showYMajorGridLines: true,
        // The card behind the chart is already a themed surface, so the chart
        // must not paint its own over the top of it.
        backgroundColor: "transparent",
        ...(overlay.length ? { overlayFields: overlay, showOverlayY2Axis: true } : {}),
        ...(Object.keys(colours).length ? { seriesColorsByField: colours } : {}),
        ...(Object.keys(dashes).length ? { lineDashStylesByField: dashes } : {}),
        ...(leftTitle ? { yAxisTitleText: leftTitle } : {}),
        ...(rightTitle ? { y2AxisTitleText: rightTitle } : {}),
        ...(xTitle && !xIsTime ? { xAxisTitleText: xTitle } : {}),
        ...toAnnotations(markers, theme),
      },
    };
  }, [series, markers, leftTitle, rightTitle, xTitle, xField, xIsTime, theme]);

  return <Line width={width} height={height} options={options} dataSources={dataSources} />;
}
