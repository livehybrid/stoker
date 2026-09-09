import type { ReactNode } from "react";
import Message from "@splunk/react-ui/Message";
import P from "@splunk/react-ui/Paragraph";
import WaitSpinner from "@splunk/react-ui/WaitSpinner";
import styled from "styled-components";
import { variables } from "@splunk/themes";

import { ApiError } from "../lib/api";
import { Button } from "./Button";

/** Neutral placeholder for an empty list / no-data state. */
const Empty = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: ${variables.spacingSmall};
  border: 1px dashed ${variables.borderColor};
  border-radius: ${variables.borderRadius};
  padding: ${variables.spacingXXLarge} ${variables.spacingLarge};
  text-align: center;
`;

const EmptyTitle = styled.p`
  margin: 0;
  font-weight: 600;
  color: ${variables.contentColorDefault};
`;

const EmptyMessage = styled.p`
  margin: 0;
  max-width: 32rem;
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
`;

export function EmptyState({
  title = "Nothing here yet",
  message,
  action,
}: {
  title?: string;
  message?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Empty>
      <EmptyTitle>{title}</EmptyTitle>
      {message && <EmptyMessage>{message}</EmptyMessage>}
      {action}
    </Empty>
  );
}

/** Error panel with the API's message and an optional retry. */
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  const status = error instanceof ApiError ? error.status : undefined;
  return (
    <Message appearance="fill" type="error">
      <Message.Title>
        {status ? `Request failed (${status})` : "Request failed"}
      </Message.Title>
      <P>{message}</P>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      )}
    </Message>
  );
}

/** Simple centred loading line for query pending states. */
const Loading = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  gap: ${variables.spacingSmall};
  padding: ${variables.spacingXXLarge} ${variables.spacingLarge};
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
`;

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <Loading>
      <WaitSpinner /> {label}
    </Loading>
  );
}
