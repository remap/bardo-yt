import { RECONNECT_BASE_DELAY_MS, nextReconnectDelay } from "./backoff.js";

// Reconnect with backoff, and re-sync on every (re)connect -- a page that was
// disconnected has missed any pushes that happened meanwhile.
export function connectSocket({ onMessage, onReconnect }) {
  let delay = RECONNECT_BASE_DELAY_MS;

  const open = () => {
    const socket = new WebSocket(`wss://${window.location.host}/ws`);

    socket.addEventListener("open", () => {
      delay = RECONNECT_BASE_DELAY_MS;
      onReconnect();
    });

    socket.addEventListener("message", (event) => {
      onMessage(JSON.parse(event.data));
    });

    socket.addEventListener("close", () => {
      setTimeout(open, delay);
      delay = nextReconnectDelay(delay);
    });

    socket.addEventListener("error", () => socket.close());
  };

  open();
}
