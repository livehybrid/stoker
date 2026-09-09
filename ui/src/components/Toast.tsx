import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import MessageBar from "@splunk/react-ui/MessageBar";
import styled from "styled-components";
import { variables } from "@splunk/themes";

/*
 * Page-level feedback.
 *
 * This used to be toasts stacked in the bottom-right corner. Splunk UI
 * deprecates that pattern outright: a message that removes itself is a message
 * a screen-reader user, or anyone who looked away, never received. It is now a
 * MessageBar at the top of the page, dismissible and persistent.
 *
 * Splunk's guidance also says not to stack MessageBars, because stacking
 * breaks the one-to-one relationship between a message and the action that
 * caused it. So only the newest is rendered, and older unread ones are counted
 * beside it rather than piling up down the page.
 *
 * The `useToast()` API is unchanged, because every mutation in the app calls
 * it and what those call sites mean ("tell the operator this happened") has
 * not changed.
 */
type ToastTone = "info" | "success" | "error";

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}

interface ToastApi {
  show: (message: string, tone?: ToastTone) => void;
  success: (message: string) => void;
  error: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const Bar = styled.div`
  position: sticky;
  top: 0;
  z-index: 10;
  padding: ${variables.spacingSmall} ${variables.spacingLarge} 0;
  background-color: ${variables.backgroundColorPage};
`;

const Count = styled.span`
  margin-left: ${variables.spacingSmall};
  color: ${variables.contentColorMuted};
  font-size: ${variables.fontSizeSmall};
`;

// A confirmation is the one kind that may reasonably disappear: the state it
// reports is visible in the page behind it. Errors stay until they are read.
const AUTO_DISMISS_MS = 8000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, tone: ToastTone = "info") => {
      const id = nextId.current;
      nextId.current += 1;
      // Capped: a mutation that fails every two seconds must not grow an
      // unbounded list of identical messages in memory.
      setItems((prev) => [...prev.slice(-9), { id, message, tone }]);
      if (tone !== "error") {
        window.setTimeout(() => remove(id), AUTO_DISMISS_MS);
      }
    },
    [remove],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (m: string) => show(m, "success"),
      error: (m: string) => show(m, "error"),
    }),
    [show],
  );

  const current = items[items.length - 1];
  const older = items.length - 1;

  return (
    <ToastContext.Provider value={api}>
      {current && (
        <Bar>
          <MessageBar
            type={current.tone}
            aria-label="Stoker notification"
            onRequestClose={() => remove(current.id)}
          >
            {current.message}
            {older > 0 && <Count>and {older} earlier message(s)</Count>}
          </MessageBar>
        </Bar>
      )}
      {children}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) {
    throw new Error("useToast() must be used inside a <ToastProvider>");
  }
  return api;
}
