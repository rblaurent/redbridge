// Stub — filled in at step 4+.
export async function getLayout() {
  const r = await fetch("/api/layout");
  return r.json();
}
