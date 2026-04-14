export interface BehaviorAssignment {
  behavior: string;
  config: Record<string, unknown>;
}

export interface DialAssignment {
  rotate: BehaviorAssignment;
  press: BehaviorAssignment;
}

export interface Layout {
  keys: Record<string, BehaviorAssignment>;
  dials: Record<string, DialAssignment>;
  strip: Record<string, BehaviorAssignment>;
}

export interface JSONSchemaProperty {
  type?: string;
  default?: unknown;
  enum?: unknown[];
  items?: JSONSchemaProperty;
  minimum?: number;
  maximum?: number;
  description?: string;
}

export interface JSONSchema {
  type: string;
  properties?: Record<string, JSONSchemaProperty>;
  required?: string[];
}

export interface BehaviorInfo {
  type_id: string;
  display_name: string;
  targets: string[];
  config_schema: JSONSchema;
}

export type ParsedTarget =
  | { kind: "key"; index: number }
  | { kind: "dial_rotate"; index: number }
  | { kind: "dial_press"; index: number }
  | { kind: "strip_region"; index: number };

export function parseTarget(id: string): ParsedTarget | null {
  const parts = id.split(":");
  if (parts[0] === "key" && parts.length === 2) {
    return { kind: "key", index: parseInt(parts[1], 10) };
  }
  if (parts[0] === "strip" && parts.length === 2) {
    return { kind: "strip_region", index: parseInt(parts[1], 10) };
  }
  if (parts[0] === "dial" && parts.length === 3) {
    if (parts[2] === "rotate") {
      return { kind: "dial_rotate", index: parseInt(parts[1], 10) };
    }
    if (parts[2] === "press") {
      return { kind: "dial_press", index: parseInt(parts[1], 10) };
    }
  }
  return null;
}

export function getAssignment(layout: Layout, t: ParsedTarget): BehaviorAssignment {
  const i = t.index.toString();
  if (t.kind === "key") return layout.keys[i];
  if (t.kind === "strip_region") return layout.strip[i];
  const d = layout.dials[i];
  return t.kind === "dial_rotate" ? d.rotate : d.press;
}

export function setAssignment(
  layout: Layout,
  t: ParsedTarget,
  a: BehaviorAssignment,
): Layout {
  const next: Layout = JSON.parse(JSON.stringify(layout));
  const i = t.index.toString();
  if (t.kind === "key") {
    next.keys[i] = a;
  } else if (t.kind === "strip_region") {
    next.strip[i] = a;
  } else if (t.kind === "dial_rotate") {
    next.dials[i].rotate = a;
  } else {
    next.dials[i].press = a;
  }
  return next;
}

export function defaultsFromSchema(schema: JSONSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, p] of Object.entries(schema.properties ?? {})) {
    if (p.default !== undefined) {
      out[k] = p.default;
      continue;
    }
    switch (p.type) {
      case "string":
        out[k] = "";
        break;
      case "integer":
      case "number":
        out[k] = 0;
        break;
      case "boolean":
        out[k] = false;
        break;
      case "array":
        out[k] = [];
        break;
      case "object":
        out[k] = {};
        break;
    }
  }
  return out;
}

export async function fetchBehaviors(): Promise<BehaviorInfo[]> {
  const r = await fetch("/api/behaviors");
  if (!r.ok) throw new Error(`GET /api/behaviors → ${r.status}`);
  return r.json();
}

export async function fetchLayout(): Promise<Layout> {
  const r = await fetch("/api/layout");
  if (!r.ok) throw new Error(`GET /api/layout → ${r.status}`);
  return r.json();
}

export async function saveLayout(layout: Layout): Promise<Layout> {
  const r = await fetch("/api/layout", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(layout),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const errs = body?.detail?.errors;
    throw new Error(`save ${r.status}: ${errs ? errs.join("; ") : r.statusText}`);
  }
  return r.json();
}
