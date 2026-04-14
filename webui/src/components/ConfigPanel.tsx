import {
  defaultsFromSchema,
  getAssignment,
  parseTarget,
  setAssignment,
  type BehaviorInfo,
  type Layout,
} from "../api";
import type { TargetId } from "./DeckMock";
import { SchemaForm } from "./SchemaForm";

interface Props {
  selected: TargetId | null;
  layout: Layout | null;
  behaviors: BehaviorInfo[];
  rendered: Record<string, string>;
  onChange: (layout: Layout) => void;
}

export function ConfigPanel({
  selected,
  layout,
  behaviors,
  rendered,
  onChange,
}: Props) {
  if (!selected) {
    return (
      <div className="p-4 text-sm text-neutral-500">
        Click a key, dial, or strip region to configure it.
      </div>
    );
  }
  const target = parseTarget(selected);
  if (!target || !layout) {
    return <div className="p-4 text-sm text-neutral-500">Loading…</div>;
  }

  const assignment = getAssignment(layout, target);
  const available = behaviors.filter((b) =>
    b.targets.includes(target.kind),
  );
  const chosen = behaviors.find((b) => b.type_id === assignment.behavior);
  const preview = rendered[selected];

  const onBehaviorChange = (newType: string) => {
    const info = behaviors.find((b) => b.type_id === newType);
    if (!info) return;
    onChange(
      setAssignment(layout, target, {
        behavior: newType,
        config: defaultsFromSchema(info.config_schema),
      }),
    );
  };

  const onConfigChange = (config: Record<string, unknown>) => {
    onChange(
      setAssignment(layout, target, {
        behavior: assignment.behavior,
        config,
      }),
    );
  };

  return (
    <div className="p-4 flex flex-col gap-5">
      <div>
        <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
          Target
        </div>
        <div className="font-mono text-sm text-neutral-200">{selected}</div>
      </div>

      {preview && (
        <div>
          <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
            Preview
          </div>
          <img
            src={`data:image/png;base64,${preview}`}
            alt=""
            className={
              target.kind === "strip_region"
                ? "w-full h-16 object-cover bg-black rounded border border-neutral-800"
                : "w-24 h-24 bg-black rounded border border-neutral-800"
            }
          />
        </div>
      )}

      <div>
        <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
          Behavior
        </div>
        <select
          value={assignment.behavior}
          onChange={(e) => onBehaviorChange(e.target.value)}
          className="w-full bg-neutral-900 border border-neutral-800 rounded px-2 py-1 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        >
          {available.map((b) => (
            <option key={b.type_id} value={b.type_id}>
              {b.display_name}
            </option>
          ))}
        </select>
      </div>

      {chosen && (
        <div>
          <div className="text-xs uppercase tracking-widest text-neutral-500 mb-1">
            Configuration
          </div>
          <SchemaForm
            schema={chosen.config_schema}
            value={assignment.config}
            onChange={onConfigChange}
          />
        </div>
      )}
    </div>
  );
}
