/*
 * Splunk packages that ship no TypeScript declarations.
 *
 * @splunk/visualization-color-palettes is published as plain JavaScript. It is
 * a transitive dependency of @splunk/visualizations that we import directly
 * (and therefore declare directly in package.json) for the default series
 * palette, so its shape is declared here rather than left as `any`.
 */
declare module "@splunk/visualization-color-palettes" {
  /** The 20-colour categorical ramp Splunk's charts use by default. */
  export const VIZ_CATEGORICAL: string[];
  export const CATEGORICAL: string[];
  export const ENTERPRISE_CATEGORICAL: string[];
  export const SCP_CATEGORICAL: string[];
}
