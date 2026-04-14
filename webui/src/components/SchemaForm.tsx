import type { JSONSchema, JSONSchemaProperty } from "../api";

interface Props {
  schema: JSONSchema;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

export function SchemaForm({ schema, value, onChange }: Props) {
  const entries = Object.entries(schema.properties ?? {});
  if (entries.length === 0) {
    return (
      <div className="text-xs text-neutral-500 italic">
        No configuration.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {entries.map(([name, prop]) => (
        <Field
          key={name}
          name={name}
          schema={prop}
          value={value[name]}
          onChange={(v) => onChange({ ...value, [name]: v })}
        />
      ))}
    </div>
  );
}

function Field({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JSONSchemaProperty;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = humanize(name);
  const current = value ?? schema.default;
  const type = schema.type ?? "string";

  if (schema.enum) {
    return (
      <Labeled label={label}>
        <select
          value={(current as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
          className={inputCls}
        >
          {schema.enum.map((opt, i) => (
            <option key={i} value={String(opt)}>
              {String(opt)}
            </option>
          ))}
        </select>
      </Labeled>
    );
  }

  if (type === "string" && /color/i.test(name)) {
    const v = typeof current === "string" ? current : "";
    const safe = /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#000000";
    return (
      <Labeled label={label}>
        <div className="flex items-center gap-2">
          <input
            type="color"
            value={safe}
            onChange={(e) => onChange(e.target.value)}
            className="h-7 w-10 bg-neutral-900 border border-neutral-800 rounded cursor-pointer"
          />
          <input
            type="text"
            value={v}
            onChange={(e) => onChange(e.target.value)}
            placeholder="#rrggbb"
            className={`${inputCls} font-mono flex-1`}
          />
        </div>
      </Labeled>
    );
  }

  if (type === "integer" || type === "number") {
    return (
      <Labeled label={label}>
        <input
          type="number"
          value={Number.isFinite(current as number) ? (current as number) : 0}
          min={schema.minimum}
          max={schema.maximum}
          step={type === "integer" ? 1 : "any"}
          onChange={(e) => onChange(Number(e.target.value))}
          className={inputCls}
        />
      </Labeled>
    );
  }

  if (type === "boolean") {
    return (
      <label className="flex items-center gap-2 text-xs text-neutral-300">
        <input
          type="checkbox"
          checked={!!current}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>{label}</span>
      </label>
    );
  }

  if (type === "array" && schema.items?.type === "string") {
    const arr = Array.isArray(current) ? (current as string[]) : [];
    return (
      <Labeled label={label} hint="comma-separated">
        <input
          type="text"
          value={arr.join(", ")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter((s) => s.length > 0),
            )
          }
          className={inputCls}
        />
      </Labeled>
    );
  }

  return (
    <Labeled label={label}>
      <input
        type="text"
        value={typeof current === "string" ? current : ""}
        onChange={(e) => onChange(e.target.value)}
        className={inputCls}
      />
    </Labeled>
  );
}

function Labeled({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="text-neutral-400 flex items-baseline gap-2">
        {label}
        {hint && <span className="text-[10px] text-neutral-600">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

const inputCls =
  "bg-neutral-900 border border-neutral-800 rounded px-2 py-1 text-sm text-neutral-100 focus:outline-none focus:border-blue-500";

function humanize(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
