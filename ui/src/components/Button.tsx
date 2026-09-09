import type { ReactNode } from "react";
import SplunkButton from "@splunk/react-ui/Button";
import type { ButtonClickHandler } from "@splunk/react-ui/Button";

/*
 * Splunk's Button, behind the variant names this app already uses.
 *
 * The mapping is deliberate rather than mechanical: "danger" is Splunk's
 * `destructive`, which is the appearance its design system reserves for an
 * action that cannot be undone, and this app uses it for abort and delete.
 *
 * The prop surface is written out rather than derived from Splunk's. Splunk
 * types Button as a union of its button and anchor forms, and spreading one
 * set of props across that union does not narrow: TypeScript ends up demanding
 * anchor event handlers on a <button>. This app never renders a Button as a
 * link, so these are the props it actually passes. Adding one is a line.
 */
type Variant = "primary" | "secondary" | "danger" | "ghost";

const APPEARANCE = {
  primary: "primary",
  secondary: "secondary",
  danger: "destructive",
  ghost: "subtle",
} as const;

export interface ButtonProps {
  variant?: Variant;
  children?: ReactNode;
  type?: "button" | "submit" | "reset";
  disabled?: boolean;
  onClick?: ButtonClickHandler;
  title?: string;
  className?: string;
  /** A @splunk/react-icons element. */
  icon?: ReactNode;
  /** Set false to stretch the button to the width of its container. */
  inline?: boolean;
}

export function Button({ variant = "secondary", children, ...rest }: ButtonProps) {
  return (
    <SplunkButton appearance={APPEARANCE[variant]} {...rest}>
      {children}
    </SplunkButton>
  );
}
