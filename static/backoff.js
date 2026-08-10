export const RECONNECT_BASE_DELAY_MS = 1000;
export const RECONNECT_MAX_DELAY_MS = 30000;

export function nextReconnectDelay(currentDelayMs, maxDelayMs = RECONNECT_MAX_DELAY_MS) {
  return Math.min(currentDelayMs * 2, maxDelayMs);
}
