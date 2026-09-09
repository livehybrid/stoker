import { useEffect, useRef, type ReactNode } from "react";
import Heading from "@splunk/react-ui/Heading";
import SidePanel from "@splunk/react-ui/SidePanel";
import styled from "styled-components";
import { variables } from "@splunk/themes";

/*
 * A right-side slide-over panel, on Splunk's SidePanel. Used by the Packs
 * preview.
 *
 * The escape key, the click-away, the slide animation and the focus handling
 * are the component's now; this file is the header layout and nothing else.
 */
const DrawerPanel = styled.div`
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
`;

const Header = styled.header`
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: ${variables.spacingMedium};
  padding: ${variables.spacingLarge};
  border-bottom: 1px solid ${variables.borderColor};
`;

const Titles = styled.div`
  min-width: 0;
`;

const Subtitle = styled.p`
  margin: 2px 0 0;
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
  overflow-wrap: anywhere;
`;

const Actions = styled.div`
  display: flex;
  align-items: center;
  gap: ${variables.spacingSmall};
  flex-shrink: 0;
`;

const Body = styled.div`
  flex: 1;
  overflow-y: auto;
  padding: ${variables.spacingLarge};
`;

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  /** A CSS width, e.g. "36rem". */
  width?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  actions,
  children,
  width = "36rem",
}: DrawerProps) {
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    if (open) {
      opener.current = document.activeElement;
    }
  }, [open]);

  const returnFocus = () => {
    const el = opener.current;
    if (el instanceof HTMLElement && document.contains(el)) {
      el.focus();
    }
  };

  return (
    <SidePanel
      open={open}
      dockPosition="right"
      onRequestClose={onClose}
      returnFocus={returnFocus}
      innerStyle={{ width: `min(${width}, 100vw)` }}
    >
      <DrawerPanel>
        <Header>
          <Titles>
            {title && <Heading level={2}>{title}</Heading>}
            {subtitle && <Subtitle>{subtitle}</Subtitle>}
          </Titles>
          {actions && <Actions>{actions}</Actions>}
        </Header>
        <Body>{children}</Body>
      </DrawerPanel>
    </SidePanel>
  );
}
