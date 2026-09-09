import type { ReactNode } from "react";

/** One line on a chart. */
export interface ChartSeries {
  /** Becomes the legend entry and the tooltip name, so it must read as prose. */
  label: string;
  /** [x, y] pairs. x is epoch milliseconds for a time chart. */
  points: Array<[number | string, number | null]>;
  /** Put this series on the second y-axis (Splunk's overlay). */
  axis?: "right";
  /**
   * Only set a colour when it carries meaning: a target rate, an error count.
   * Everything else should take Splunk's own categorical palette, because a
   * chart that invents its own colours for ordinary series is the
   * inconsistency this replaced.
   */
  color?: string;
  dashed?: boolean;
}

/** A moment worth marking on the x-axis. */
export interface ChartMarker {
  /** Epoch milliseconds. */
  at: number;
  label: string;
  kind?: "warning" | "error" | "info";
}

export interface ChartProps {
  series: ChartSeries[];
  markers?: ChartMarker[];
  leftTitle?: string;
  rightTitle?: string;
  xTitle?: string;
  /** "time" treats x as epoch milliseconds; "category" as a discrete label. */
  xKind?: "time" | "category";
  /** Shown instead of the chart when there is nothing to draw. */
  empty?: ReactNode;
  height?: number;
}
