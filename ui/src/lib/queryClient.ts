import { QueryClient } from "@tanstack/react-query";

// Shared TanStack Query client. Live views poll at 5 s (design section 10:
// "Polling via React Query at 5 s for live runs"); individual queries opt in
// with `refetchInterval: POLL_MS`. Defaults here are conservative so static
// lists do not hammer the API.
export const POLL_MS = 5000;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
