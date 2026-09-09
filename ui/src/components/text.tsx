/*
 * Small text primitives, from theme tokens.
 *
 * These replace the Tailwind colour utilities that were scattered through the
 * app (`text-slate-500`, `text-red-400`, `font-mono`). Going through tokens is
 * what makes the light colour scheme work: nothing here knows a hex value, so
 * switching the theme switches this with it.
 */
import styled from "styled-components";
import { variables } from "@splunk/themes";

/** Secondary text: hints, counts, "nothing here yet". */
export const Muted = styled.span<{ $small?: boolean }>`
  color: ${variables.contentColorMuted};
  font-size: ${(props) => (props.$small ? variables.fontSizeSmall : "inherit")};
`;

/** Identifiers, URLs, SPL and anything else that must line up. */
export const Mono = styled.span<{ $break?: boolean }>`
  font-family: ${variables.monoFontFamily};
  word-break: ${(props) => (props.$break ? "break-all" : "normal")};
`;

/** A short explanatory aside, set off from the thing it explains. */
export const Note = styled.div`
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
  border-left: 2px solid ${variables.borderColor};
  padding: 2px 0 2px ${variables.spacingSmall};
  margin: ${variables.spacingSmall} 0;
`;

/** A line whose colour carries the verdict. */
export const Positive = styled.span`
  color: ${variables.successColor};
  font-size: ${variables.fontSizeSmall};
`;

export const Negative = styled.span`
  color: ${variables.errorColor};
  font-size: ${variables.fontSizeSmall};
`;

/** The common horizontal group: buttons, chips, filters. */
export const Inline = styled.div<{ $gap?: "small" | "medium" }>`
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: ${(props) => (props.$gap === "medium" ? variables.spacingMedium : variables.spacingSmall)};
`;

/** The common vertical stack. */
export const Stack = styled.div<{ $gap?: "small" | "medium" | "large" }>`
  display: flex;
  flex-direction: column;
  gap: ${(props) =>
    props.$gap === "large"
      ? variables.spacingLarge
      : props.$gap === "medium"
        ? variables.spacingMedium
        : variables.spacingSmall};
`;

/** A responsive form grid: collapses to one column rather than scrolling. */
export const Grid = styled.div<{ $min?: string }>`
  display: grid;
  grid-template-columns: ${(props) =>
    `repeat(auto-fit, minmax(${props.$min || "240px"}, 1fr))`};
  gap: ${variables.spacingMedium};
`;

/** A flex row that pushes its two halves apart: title on the left, actions right. */
export const Between = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${variables.spacingSmall};
  flex-wrap: wrap;
`;

/** Small print. */
export const Small = styled.span`
  font-size: ${variables.fontSizeSmall};
`;

/** An emphasised value inside a line of prose. */
export const Strong = styled.span`
  font-weight: 600;
  color: ${variables.contentColorDefault};
`;

/** The little all-caps key above a value, as Splunk labels a stat. */
export const Label = styled.div`
  font-size: ${variables.fontSizeSmall};
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: ${variables.contentColorMuted};
`;

/** Preformatted output: log tails, YAML, JSON, a spec snapshot. */
export const Pre = styled.pre`
  margin: 0;
  overflow: auto;
  max-height: 24rem;
  padding: ${variables.spacingSmall};
  border: 1px solid ${variables.borderColor};
  border-radius: ${variables.borderRadius};
  background-color: ${variables.backgroundColorPage};
  color: ${variables.contentColorDefault};
  font-family: ${variables.monoFontFamily};
  font-size: ${variables.fontSizeSmall};
  white-space: pre-wrap;
  word-break: break-word;
`;

/** A form: a vertical stack of fields with a consistent rhythm. */
export const Form = styled.form`
  display: flex;
  flex-direction: column;
  gap: ${variables.spacingMedium};
`;

/** A bulleted list, for the "why this was rejected" kind of detail. */
export const Bullets = styled.ul`
  margin: ${variables.spacingXSmall} 0 0;
  padding-left: ${variables.spacingLarge};
  list-style: disc;
  display: flex;
  flex-direction: column;
  gap: 2px;
`;

/** An inset surface inside a Card: a preview, a summary, a nested detail. */
export const Panel = styled.div`
  border: 1px solid ${variables.borderColor};
  border-radius: ${variables.borderRadius};
  background-color: ${variables.backgroundColorSection};
  padding: ${variables.spacingMedium};
`;

/** A warning-coloured value inside a line of prose. */
export const Warn = styled.span`
  color: ${variables.warningColor};
`;

/** A short inline callout: a validation failure, a "note this" aside. */
export const Callout = styled.div<{ $tone?: "error" | "info" | "warning" }>`
  border: 1px solid
    ${(props) =>
      props.$tone === "error"
        ? variables.errorColor
        : props.$tone === "warning"
          ? variables.warningColor
          : variables.borderColor};
  border-radius: ${variables.borderRadius};
  padding: ${variables.spacingSmall} ${variables.spacingMedium};
  font-size: ${variables.fontSizeSmall};
  color: ${(props) =>
    props.$tone === "error"
      ? variables.errorColor
      : props.$tone === "warning"
        ? variables.warningColor
        : variables.contentColorDefault};
`;

/** A row of chips or tags that wraps. */
export const Tags = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: ${variables.spacingXSmall};
`;

/** A right-aligned row: the buttons at the foot of a form or a card. */
export const EndRow = styled.div`
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: ${variables.spacingSmall};
  flex-wrap: wrap;
`;

/** The action bar that stays put at the foot of a long form. */
export const StickyBar = styled.div`
  position: sticky;
  bottom: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${variables.spacingMedium};
  flex-wrap: wrap;
  border-top: 1px solid ${variables.borderColor};
  background-color: ${variables.backgroundColorPage};
  padding: ${variables.spacingMedium} 0;
`;

/** A full-viewport centring wrapper: the sign-in screen. */
export const Centre = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: ${variables.spacingLarge};
`;

/** A column capped to a readable width. */
export const Narrow = styled.div<{ $max?: string }>`
  width: 100%;
  max-width: ${(props) => props.$max || "24rem"};
  display: flex;
  flex-direction: column;
  gap: ${variables.spacingLarge};
`;

/** A headline figure: an events/s total, a run count. */
export const BigNumber = styled.div<{ $size?: "large" | "medium" }>`
  font-size: ${(props) =>
    props.$size === "medium" ? variables.fontSizeXLarge : variables.fontSizeXXLarge};
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: ${variables.contentColorDefault};
`;

/** A bounded, scrolling list: a pack picker, a long selection. */
export const ScrollList = styled.div`
  max-height: 24rem;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: ${variables.spacingXSmall};
  padding-right: ${variables.spacingXSmall};
`;
