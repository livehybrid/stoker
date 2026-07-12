// Shared TanStack Query key for the users list, so the page, the create form and
// the row actions all invalidate the same cache entry.
export const USERS_QUERY_KEY = ["users"] as const;
