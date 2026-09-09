/*
 * The Splunk theme, and the one control the operator has over it.
 *
 * Enterprise rather than Prisma, because Stoker drives load at Splunk
 * Enterprise and this console sits beside Splunk Web; compact because it is a
 * page full of tables of numbers. The colour scheme starts from the browser's
 * preference, is remembered per browser, and is the only thing here a user can
 * change.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { SplunkThemeProvider } from "@splunk/themes";

type ColorScheme = "dark" | "light";

const STORAGE_KEY = "stoker.colorScheme";

interface ThemeApi {
  colorScheme: ColorScheme;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeApi>({
  colorScheme: "dark",
  toggle: () => {},
});

function readScheme(): ColorScheme {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "dark" || stored === "light") {
      return stored;
    }
  } catch {
    // A browser with storage disabled is not a reason to fail to render.
  }
  return window.matchMedia?.("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [colorScheme, setColorScheme] = useState<ColorScheme>(readScheme);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, colorScheme);
    } catch {
      // Not worth a message: the theme simply will not be remembered.
    }
    // Tells the browser which scrollbars and form controls to paint, for the
    // few things the theme does not reach.
    document.documentElement.style.colorScheme = colorScheme;
  }, [colorScheme]);

  const toggle = useCallback(
    () => setColorScheme((s) => (s === "dark" ? "light" : "dark")),
    [],
  );
  const api = useMemo(() => ({ colorScheme, toggle }), [colorScheme, toggle]);

  return (
    <ThemeContext.Provider value={api}>
      <SplunkThemeProvider family="enterprise" colorScheme={colorScheme} density="compact">
        {children}
      </SplunkThemeProvider>
    </ThemeContext.Provider>
  );
}

export const useColorScheme = () => useContext(ThemeContext);
