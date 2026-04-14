import { useEffect, useRef, useState } from "react";

export type WSMsg =
  | { type: "render"; target: string; png_b64: string }
  | { type: "input"; target: string; event: string; [k: string]: unknown };

export function useWS(onMessage: (msg: WSMsg) => void) {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    let closed = false;
    let retry: number | null = null;
    let sock: WebSocket | null = null;

    const connect = () => {
      if (closed) return;
      const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
      const s = new WebSocket(url);
      sock = s;

      s.onopen = () => {
        if (closed || sock !== s) return;
        setConnected(true);
      };
      s.onclose = () => {
        if (closed || sock !== s) return;
        setConnected(false);
        retry = window.setTimeout(connect, 1000);
      };
      s.onerror = () => s.close();
      s.onmessage = (ev) => {
        if (closed || sock !== s) return;
        try {
          handlerRef.current(JSON.parse(ev.data) as WSMsg);
        } catch {
          /* ignore malformed */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      sock?.close();
    };
  }, []);

  return { connected };
}
