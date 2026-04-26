import { type DeviceSettings } from "../api";

interface Props {
  settings: DeviceSettings;
  onChange: (settings: DeviceSettings) => void;
}

const SCREENSAVER_OPTIONS = [
  { value: 0, label: "Off" },
  { value: 1, label: "1 min" },
  { value: 5, label: "5 min" },
  { value: 10, label: "10 min" },
  { value: 15, label: "15 min" },
  { value: 30, label: "30 min" },
  { value: 60, label: "60 min" },
];

const TICK_HZ_OPTIONS = [1, 2, 4, 8, 15, 30];

export function SettingsPanel({ settings, onChange }: Props) {
  return (
    <div className="p-4 flex flex-col gap-5">
      <div>
        <div className="text-xs uppercase tracking-widest text-neutral-500 mb-2">
          Brightness
        </div>
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={0}
            max={100}
            value={settings.brightness}
            onChange={(e) =>
              onChange({ ...settings, brightness: parseInt(e.target.value, 10) })
            }
            className="flex-1 accent-blue-500"
          />
          <span className="text-sm font-mono text-neutral-300 w-10 text-right">
            {settings.brightness}%
          </span>
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
          Screensaver
        </div>
        <select
          value={settings.screensaver_minutes}
          onChange={(e) =>
            onChange({
              ...settings,
              screensaver_minutes: parseInt(e.target.value, 10),
            })
          }
          className="w-full bg-neutral-900 border border-neutral-800 rounded px-2 py-1 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        >
          {SCREENSAVER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <div className="text-xs text-neutral-600 mt-1">
          Dim display after inactivity
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
          Frame rate
        </div>
        <select
          value={settings.tick_hz}
          onChange={(e) =>
            onChange({ ...settings, tick_hz: parseInt(e.target.value, 10) })
          }
          className="w-full bg-neutral-900 border border-neutral-800 rounded px-2 py-1 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        >
          {TICK_HZ_OPTIONS.map((hz) => (
            <option key={hz} value={hz}>
              {hz} Hz
            </option>
          ))}
        </select>
        <div className="text-xs text-neutral-600 mt-1">
          Render tick rate for animations
        </div>
      </div>
    </div>
  );
}
