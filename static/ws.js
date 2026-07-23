/**
 * ws.js — Socket.IO shim using native WebSocket.
 * Exposes `socket` with .on(event, fn) and .emit(event, data)
 * matching envelope: { type: "event_name", data: {...} }
 */
(function () {
  const handlers = {};
  const queue = [];
  let ws = null;
  let reconnectDelay = 1000;

  function connect() {
    const url = `ws://${location.host}/ws`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      reconnectDelay = 1000;
      while (queue.length) ws.send(queue.shift());
      fire("connect", {});
    };

    ws.onmessage = (ev) => {
      try {
        const env = JSON.parse(ev.data);
        fire(env.type, env.data);
      } catch (e) {
        console.warn("ws.js: bad message", ev.data, e);
      }
    };

    ws.onclose = () => {
      fire("disconnect", {});
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    };

    ws.onerror = (e) => console.error("ws.js error:", e);
  }

  function fire(event, data) {
    (handlers[event] || []).forEach((fn) => fn(data));
  }

  const socket = {
    on(event, fn) {
      if (!handlers[event]) handlers[event] = [];
      handlers[event].push(fn);
    },
    emit(event, data) {
      const msg = JSON.stringify({ type: event, data: data || {} });
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(msg);
      } else {
        queue.push(msg);
      }
    },
    off(event, fn) {
      if (!fn) { delete handlers[event]; return; }
      handlers[event] = (handlers[event] || []).filter((f) => f !== fn);
    },
  };

  window.socket = socket;
  window.io = () => socket;

  connect();
})();
