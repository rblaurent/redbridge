// Step-5 placeholder: just shows the selected target. Real form lands in step 7.
import type { TargetId } from "./DeckMock";

interface Props {
  selected: TargetId | null;
}

export function ConfigPanel({ selected }: Props) {
  return (
    <div className="p-4 flex flex-col gap-3">
      <div className="text-xs uppercase tracking-widest text-neutral-500">
        Selection
      </div>
      {selected ? (
        <div className="font-mono text-sm bg-neutral-900 border border-neutral-800 rounded px-3 py-2">
          {selected}
        </div>
      ) : (
        <div className="text-sm text-neutral-500">
          Click a key, dial, or strip region.
        </div>
      )}
    </div>
  );
}
