import { useCallback, useEffect, useRef, useState } from "react";
import { DeckMock, type TargetId } from "./components/DeckMock";
import { ConfigPanel } from "./components/ConfigPanel";
import { useWS, type WSMsg } from "./hooks/useWS";
import { SettingsPanel } from "./components/SettingsPanel";
import {
  fetchBehaviors,
  fetchLayout,
  fetchSettings,
  saveDeviceSettings,
  saveLayout,
  type BehaviorInfo,
  type DeviceSettings,
  type Layout,
} from "./api";

type SidebarTab = "layout" | "device";

export default function App() {
  const [selected, setSelected] = useState<TargetId | null>(null);
  const [rendered, setRendered] = useState<Record<string, string>>({});
  const [flash, setFlash] = useState<Record<string, number>>({});
  const flashTimers = useRef<Record<string, number>>({});

  const [layout, setLayout] = useState<Layout | null>(null);
  const [originalLayout, setOriginalLayout] = useState<Layout | null>(null);
  const [behaviors, setBehaviors] = useState<BehaviorInfo[]>([]);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("layout");
  const [settings, setSettings] = useState<DeviceSettings | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);

  const [mirrorOn, setMirrorOn] = useState(true);
  const mirrorOnRef = useRef(mirrorOn);
  mirrorOnRef.current = mirrorOn;

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchLayout(), fetchBehaviors(), fetchSettings()])
      .then(([l, b, s]) => {
        if (cancelled) return;
        setLayout(l);
        setOriginalLayout(l);
        setBehaviors(b);
        setSettings(s);
      })
      .catch((e) => !cancelled && setLoadError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const onMessage = useCallback((msg: WSMsg) => {
    if (msg.type === "render") {
      if (!mirrorOnRef.current) return;
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

  const dirty =
    layout !== null &&
    originalLayout !== null &&
    JSON.stringify(layout) !== JSON.stringify(originalLayout);

  const onSave = async () => {
    if (!layout || !dirty) return;
    setSaving(true);
    try {
      const saved = await saveLayout(layout);
      setLayout(saved);
      setOriginalLayout(saved);
    } catch (e) {
      alert(String(e));
    } finally {
      setSaving(false);
    }
  };

  const onReload = async () => {
    try {
      const l = await fetchLayout();
      setLayout(l);
      setOriginalLayout(l);
    } catch (e) {
      alert(String(e));
    }
  };

  const onSettingsChange = async (next: DeviceSettings) => {
    setSettings(next);
    setSettingsSaving(true);
    try {
      await saveDeviceSettings(next);
    } catch (e) {
      alert(String(e));
    } finally {
      setSettingsSaving(false);
    }
  };

  return (
    <div className="h-screen flex flex-col bg-neutral-950 text-neutral-100">
      <header className="h-12 border-b border-neutral-800 flex items-center px-4 text-sm gap-4">
        <span className="font-medium">redbridge</span>
        <span className="text-xs text-neutral-500 font-mono">
          Stream Deck Plus configurator
        </span>

        <div className="ml-auto flex items-center gap-4 text-xs font-mono">
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={mirrorOn}
              onChange={(e) => setMirrorOn(e.target.checked)}
              className="accent-blue-500"
            />
            <span className="text-neutral-400">live mirror</span>
          </label>

          <div className="flex items-center gap-2">
            <span
              className={
                "w-2 h-2 rounded-full " +
                (connected ? "bg-green-500" : "bg-neutral-600")
              }
            />
            <span className="text-neutral-400">
              {connected ? "ws" : "offline"}
            </span>
          </div>

          <button
            type="button"
            onClick={onReload}
            disabled={saving}
            className="px-3 py-1 rounded border border-neutral-800 bg-neutral-900 hover:bg-neutral-800 disabled:opacity-50"
          >
            Reload
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={!dirty || saving}
            className={
              "px-3 py-1 rounded border " +
              (dirty
                ? "border-blue-500 bg-blue-600 hover:bg-blue-500 text-white"
                : "border-neutral-800 bg-neutral-900 text-neutral-500")
            }
          >
            {saving ? "Saving…" : dirty ? "Save*" : "Save"}
          </button>
        </div>
      </header>

      {loadError && (
        <div className="bg-red-950 border-b border-red-800 text-xs px-4 py-2 text-red-200 font-mono">
          {loadError}
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        <main className="flex-1 flex items-center justify-center">
          <DeckMock
            selected={selected}
            onSelect={setSelected}
            rendered={rendered}
            flash={flash}
          />
        </main>
        <aside className="w-96 border-l border-neutral-800 flex flex-col min-h-0">
          <div className="flex border-b border-neutral-800 text-xs font-mono">
            <button
              type="button"
              onClick={() => setSidebarTab("layout")}
              className={
                "flex-1 px-4 py-2 " +
                (sidebarTab === "layout"
                  ? "text-neutral-100 border-b-2 border-blue-500"
                  : "text-neutral-500 hover:text-neutral-300")
              }
            >
              Layout
            </button>
            <button
              type="button"
              onClick={() => setSidebarTab("device")}
              className={
                "flex-1 px-4 py-2 " +
                (sidebarTab === "device"
                  ? "text-neutral-100 border-b-2 border-blue-500"
                  : "text-neutral-500 hover:text-neutral-300") +
                (settingsSaving ? " opacity-60" : "")
              }
            >
              Device
            </button>
          </div>
          <div className="flex-1 overflow-y-auto">
            {sidebarTab === "layout" ? (
              <ConfigPanel
                selected={selected}
                layout={layout}
                behaviors={behaviors}
                rendered={rendered}
                onChange={setLayout}
              />
            ) : settings ? (
              <SettingsPanel settings={settings} onChange={onSettingsChange} />
            ) : (
              <div className="p-4 text-sm text-neutral-500">Loading…</div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
