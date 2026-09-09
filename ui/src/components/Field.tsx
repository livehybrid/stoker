import type { ReactNode } from "react";
import ControlGroup from "@splunk/react-ui/ControlGroup";
import SplunkSelect from "@splunk/react-ui/Select";
import Text from "@splunk/react-ui/Text";

/*
 * Form controls, from Splunk UI.
 *
 * `Field` is Splunk's ControlGroup: it owns the label, the help text and the
 * error, and wires the accessibility relationships between them and whatever
 * control it wraps, which the hand-rolled <label> it replaced did not. An
 * error replaces the hint rather than stacking under it: when something is
 * wrong, the thing to read is what is wrong.
 *
 * Note the change of signature that comes with these controls. Splunk's call
 * `onChange(event, { value })` rather than leaving the caller to read
 * `event.target.value`. That is the library's contract and it is worth having:
 * a Number gives back a number, and a Select gives back the option's value
 * rather than a string scraped off a DOM node.
 */
interface FieldProps {
  label: string;
  hint?: ReactNode;
  error?: string;
  children: ReactNode;
}

export function Field({ label, hint, error, children }: FieldProps) {
  return (
    <ControlGroup
      label={label}
      labelPosition="top"
      help={error ? undefined : hint}
      error={error}
    >
      {children}
    </ControlGroup>
  );
}

export const TextInput = Text;
export const Select = SplunkSelect;
