export type TargetId = string;

interface Props {
  selected: TargetId | null;
  onSelect: (id: TargetId) => void;
  rendered: Record<string, string>;
  flash: Record<string, number>;
}

export function DeckMock({ selected, onSelect, rendered, flash }: Props) {
  return (
    <div className="flex flex-col items-center gap-6 p-8 rounded-2xl bg-neutral-900 border border-neutral-800 shadow-2xl">
      <div className="text-xs uppercase tracking-widest text-neutral-500">
        Stream Deck Plus
      </div>

      <div className="grid grid-cols-4 gap-3">
        {Array.from({ length: 8 }, (_, i) => {
          const id = `key:${i}`;
          return (
            <Key
              key={i}
              index={i}
              selected={selected === id}
              flashing={!!flash[id]}
              png={rendered[id]}
              onClick={() => onSelect(id)}
            />
          );
        })}
      </div>

      <div className="grid grid-cols-4 w-full rounded-md overflow-hidden border border-neutral-700">
        {[0, 1, 2, 3].map((i) => {
          const id = `strip:${i}`;
          return (
            <StripRegion
              key={i}
              index={i}
              last={i === 3}
              selected={selected === id}
              flashing={!!flash[id]}
              png={rendered[id]}
              onClick={() => onSelect(id)}
            />
          );
        })}
      </div>

      <div className="grid grid-cols-4 w-full">
        {[0, 1, 2, 3].map((i) => (
          <Dial key={i} index={i} selected={selected} flash={flash} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}

function FlashOverlay({ shape }: { shape: "rect" | "circle" }) {
  const round = shape === "circle" ? "rounded-full" : "rounded-md";
  return (
    <span
      className={
        "absolute inset-0 pointer-events-none ring-4 ring-inset ring-yellow-400 " +
        round
      }
    />
  );
}

function Key({
  index,
  selected,
  flashing,
  png,
  onClick,
}: {
  index: number;
  selected: boolean;
  flashing: boolean;
  png?: string;
  onClick: () => void;
}) {
  const border = selected
    ? "ring-2 ring-blue-400"
    : "ring-1 ring-neutral-700";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative w-24 h-24 rounded-md overflow-hidden bg-black ${border}`}
    >
      {png ? (
        <img
          src={`data:image/png;base64,${png}`}
          alt=""
          className="w-full h-full object-cover pointer-events-none"
        />
      ) : (
        <span className="absolute inset-0 flex items-center justify-center text-xs font-mono text-neutral-500">
          {index}
        </span>
      )}
      {flashing && <FlashOverlay shape="rect" />}
    </button>
  );
}

function StripRegion({
  index,
  last,
  selected,
  flashing,
  png,
  onClick,
}: {
  index: number;
  last: boolean;
  selected: boolean;
  flashing: boolean;
  png?: string;
  onClick: () => void;
}) {
  const border = last ? "" : "border-r border-neutral-700";
  const sel = selected ? "ring-2 ring-inset ring-blue-400" : "";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative h-14 bg-black ${border} ${sel}`}
    >
      {png ? (
        <img
          src={`data:image/png;base64,${png}`}
          alt=""
          className="w-full h-full object-cover pointer-events-none"
        />
      ) : (
        <span className="absolute inset-0 flex items-center justify-center text-[11px] font-mono text-neutral-500">
          strip {index}
        </span>
      )}
      {flashing && <FlashOverlay shape="rect" />}
    </button>
  );
}

function Dial({
  index,
  selected,
  flash,
  onSelect,
}: {
  index: number;
  selected: TargetId | null;
  flash: Record<string, number>;
  onSelect: (id: TargetId) => void;
}) {
  const rotateId = `dial:${index}:rotate`;
  const pressId = `dial:${index}:press`;
  const rotateSel = selected === rotateId;
  const pressSel = selected === pressId;
  const rotateFlash = !!flash[rotateId];
  const pressFlash = !!flash[pressId];
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative w-16 h-16">
        <button
          type="button"
          aria-label={`dial ${index} rotate`}
          onClick={() => onSelect(rotateId)}
          className={
            "absolute inset-0 rounded-full border-4 transition-colors " +
            (rotateSel
              ? "border-blue-400 bg-blue-950/30"
              : "border-neutral-600 bg-neutral-800 hover:border-neutral-400")
          }
        />
        <button
          type="button"
          aria-label={`dial ${index} press`}
          onClick={() => onSelect(pressId)}
          className={
            "absolute inset-[10px] rounded-full transition-colors " +
            (pressSel
              ? "bg-blue-500"
              : "bg-neutral-700 hover:bg-neutral-500")
          }
        />
        {rotateFlash && <FlashOverlay shape="circle" />}
        {pressFlash && (
          <span className="absolute inset-[10px] pointer-events-none rounded-full ring-4 ring-inset ring-yellow-400" />
        )}
      </div>
      <div className="text-[10px] font-mono text-neutral-500">dial {index}</div>
    </div>
  );
}
