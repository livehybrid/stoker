import type { ReactNode } from "react";
import Heading from "@splunk/react-ui/Heading";
import styled from "styled-components";
import { variables } from "@splunk/themes";

/** Standard page title row: heading, optional subtitle and right-side actions. */
const Row = styled.div`
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: ${variables.spacingLarge};
  margin-bottom: ${variables.spacingLarge};
`;

const Subtitle = styled.p`
  margin: 2px 0 0;
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
`;

const Actions = styled.div`
  display: flex;
  align-items: center;
  gap: ${variables.spacingSmall};
  flex-wrap: wrap;
`;

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <Row>
      <div>
        <Heading level={1}>{title}</Heading>
        {subtitle && <Subtitle>{subtitle}</Subtitle>}
      </div>
      {actions && <Actions>{actions}</Actions>}
    </Row>
  );
}
