// Read the per-browser anonymous id that the Base44 SDK already uses
// for its own analytics session. The SDK attaches the same value as the
// `X-Base44-Anonymous-Id` header on every unauthenticated axios call
// (see @base44/sdk/dist/utils/axios-client.js → `getAnalyticsSessionId`),
// so the backend's `get_current_user_optional` will stamp any row this
// browser creates with this id — and we can use the same value on the
// client to filter "My Skills" and other per-user views without
// requiring the user to log in.
//
// We deliberately do NOT generate the id here. The SDK owns the
// storage key (`base44_analytics_session_id`); reading it just gives
// the page-level filters something stable to compare against.
//
// If the SDK ever changes that key, this file is the single point of
// failure and is easy to update.

const STORAGE_KEY = "base44_analytics_session_id";

export function getAnonymousId() {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // localStorage can throw in private mode / sandboxed iframes.
    return null;
  }
}
