import { useCallback, useRef, useState } from "react";
import { DeckMock, type TargetId } from "./components/DeckMock";
import { ConfigPanel } from "./components/ConfigPanel";
import { useWS, type WSMsg } from "./hooks/useWS";

export default function App() {
  const [selected, setSelected] = useState<TargetId | null>(null);
  const [rendered, setRendered] = useState<Record<string, string>>({});
  const [flash, setFlash] = useState<Record<string, number>>({});
  const flashTimers = useRef<Record<string, number>>({});

  const onMessage = useCallback((msg: WSMsg) => {
    if (msg.type === "render") {
      setRendered((r) => ({ ...r, [msg.target]: msg.png_b64 }));
    } else if (msg.type === "input") {
      setFlash((f) => ({ ...f, [msg.target]: Date.now() }));
      const existing = flashTimers.current[msg.target];
      if (existing) clearTimeout(existing);
      flashTimers.current[msg.target] = window.setTimeout(() => {
        setFlash((f) => {
          const { [msg.target]: _drop, ...rest } = f;
          return rest;
        });
        delete flashTimers.current[msg.target];
      }, 400);
    }
  }, []);

  const { connected } = useWS(onMessage);

  return (
    <div className="h-screen flex flex-col bg-neutral-950 text-neutral-100">
      <header className="h-12 border-b border-neutral-800 flex items-center px-4 text-sm">
        <span className="font-medium">redbridge</span>
        <span className="ml-3 text-xs text-neutral-500 font-mono">
          Stream Deck Plus configurator
        </span>
        <span className="ml-auto flex items-center gap-2 text-xs font-mono">
          <span
            className={
              "w-2 h-2 rounded-full " +
              (connected ? "bg-green-500" : "bg-neutral-600")
            }
          />
          <span className="text-neutral-400">
            {connected ? "live mirror" : "offline"}
          </span>
        </span>
      </header>

      <div className="flex-1 flex min-h-0">
        <main className="flex-1 flex items-center justify-center">
          <DeckMock
            selected={selected}
            onSelect={setSelected}
            rendered={rendered}
            flash={flash}
          />
        </main>
        <aside className="w-96 border-l border-neutral-800 overflow-y-auto">
          <ConfigPanel selected={selected} />
        </aside>
      </div>
    </div>
  );
}
