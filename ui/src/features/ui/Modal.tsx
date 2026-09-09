import { useEffect, useRef, type ReactNode } from "react";
import SplunkModal from "@splunk/react-ui/Modal";

/*
 * A centred modal dialog, on Splunk's Modal.
 *
 * Used by the Repos "Register repo" form and the one-time webhook-secret
 * reveal. Escape and the close button come with the component now, as does
 * focus trapping and restoring focus to whatever opened it, none of which the
 * hand-rolled overlay did.
 *
 * `dismissible: false` is what the secret reveal uses: clicking away from a
 * value you can never see again should not be how you lose it. Splunk's Modal
 * already refuses click-away by default, so this only decides whether a close
 * button is offered.
 */
interface ModalProps {
  open: boolean;
  onClose: () => void;
  /** Splunk's Modal.Header takes a string, not arbitrary nodes. */
  title?: string;
  children?: ReactNode;
  footer?: ReactNode;
  /** A CSS width, e.g. "560px". */
  width?: string;
  dismissible?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = "560px",
  dismissible = true,
}: ModalProps) {
  // Splunk's Modal must return focus to whatever opened it. These are opened
  // from a button the page owns rather than one this component can hold a ref
  // to, so the element that had focus when the dialog opened is captured and
  // focused again on close: the same outcome by a different route.
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
    <SplunkModal
      open={open}
      onRequestClose={dismissible ? onClose : undefined}
      returnFocus={returnFocus}
      style={{ width: `min(${width}, 94vw)` }}
    >
      {/* The close button belongs to Modal, not to its header: it is rendered
          because Modal was given onRequestClose above, which only happens when
          the dialog is dismissible. */}
      {title && <SplunkModal.Header title={title} />}
      <SplunkModal.Body>{children}</SplunkModal.Body>
      {footer && <SplunkModal.Footer>{footer}</SplunkModal.Footer>}
    </SplunkModal>
  );
}
