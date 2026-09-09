/*
 * Charts, drawn by Splunk's own visualization library.
 *
 * These are @splunk/visualizations Line and Area charts, the same components
 * Dashboard Studio renders, so the axes, gridlines, legend, tooltips, hover
 * behaviour and series palette are the ones an operator already knows from
 * Splunk Web rather than an approximation of them. They replaced recharts,
 * which drew perfectly good charts that looked like nothing else in Splunk.
 *
 * This file holds the sizing and the empty state. The mapping onto Splunk's
 * dataSource contract is in SplunkChart, which is loaded lazily because the
 * chart library is large and most pages have no chart on them. Callers pass
 * plain `[x, y]` arrays and never see that contract.
 */
import { Suspense, lazy, useRef } from "react";
import WaitSpinner from "@splunk/react-ui/WaitSpinner";
import styled from "styled-components";
import useResizeObserver from "@splunk/react-ui/useResizeObserver";
import { VIZ_CATEGORICAL } from "@splunk/visualization-color-palettes";
import { variables } from "@splunk/themes";

import type { ChartProps } from "./types";

const SplunkChart = lazy(() => import("./SplunkChart"));

export type { ChartMarker, ChartProps, ChartSeries } from "./types";

/**
 * Splunk's default series palette, which is what the charts use when left to
 * choose. Exposed so a page can pin one series to the same colour as another
 * (an actual rate and its target belong together, one dashed).
 */
export const PALETTE = VIZ_CATEGORICAL;

const Measured = styled.div`
  width: 100%;
`;

const Placeholder = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  gap: ${variables.spacingSmall};
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
  text-align: center;
`;

/**
 * A chart at the width of its container.
 *
 * The Splunk charts want a pixel width rather than a percentage, so the
 * container is measured with react-ui's own resize hook and the chart is
 * re-rendered when it changes. Before the first measurement there is no width
 * to draw at; guessing one and correcting it makes the chart visibly jump, so
 * it waits a frame instead.
 */
export function LineChart({ height = 240, empty, series, ...rest }: ChartProps) {
  const box = useRef<HTMLDivElement | null>(null);
  const { width } = useResizeObserver(box);
  const drawable = series.some((s) => s.points.length >= 2);

  if (!drawable) {
    return <Placeholder style={{ height }}>{empty ?? "Nothing to plot yet."}</Placeholder>;
  }

  return (
    <Measured ref={box}>
      {width > 0 && (
        <Suspense
          fallback={
            <Placeholder style={{ height }}>
              <WaitSpinner /> Loading the chart library…
            </Placeholder>
          }
        >
          <SplunkChart {...rest} series={series} width={width} height={height} />
        </Suspense>
      )}
    </Measured>
  );
}
