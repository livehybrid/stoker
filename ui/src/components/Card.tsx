import type { ReactNode } from "react";
import SplunkCard from "@splunk/react-ui/Card";
import styled from "styled-components";

/*
 * A surface panel with an optional header row (title + actions).
 *
 * Splunk's Card. `flush` removes the body padding, which is what a table
 * wants: a Table inside a padded Card looks inset and gives up a lot of width
 * on pages whose whole job is showing wide tables of numbers.
 */
const FullWidth = styled(SplunkCard)`
  width: 100%;
`;

const Body = styled(SplunkCard.Body)<{ $flush?: boolean }>`
  padding: ${(props) => (props.$flush ? "0" : undefined)};
  overflow-x: auto;
`;

interface CardProps {
  title?: ReactNode;
  actions?: ReactNode;
  className?: string;
  flush?: boolean;
  children?: ReactNode;
}

export function Card({ title, actions, className, flush, children }: CardProps) {
  return (
    <FullWidth className={className}>
      {(title || actions) && <SplunkCard.Header title={title}>{actions}</SplunkCard.Header>}
      <Body $flush={flush}>{children}</Body>
    </FullWidth>
  );
}
