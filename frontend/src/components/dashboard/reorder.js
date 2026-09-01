export function reorderWidgets(widgets, fromId, toId) {
  const from = widgets.findIndex((w) => w.id === fromId);
  const to = widgets.findIndex((w) => w.id === toId);
  if (from === -1 || to === -1 || from === to) return widgets;
  const next = [...widgets];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
