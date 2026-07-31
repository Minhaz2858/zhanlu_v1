// Decide what a widget click should do. Pure — unit-tested in isolation.
// Returns null, {kind:'drill',widgetId,value}, or {kind:'filter',token,value}.
// Drill takes precedence over dimension filtering when both target the same
// column (a widget's options.drill is its own configured click behavior).

export function resolveClick(widget, column, value) {
  if (value == null || value === '') return null;
  const opts = widget?.options || {};
  const drill = opts.drill;
  if (drill && drill.value_column === column) {
    return { kind: 'drill', widgetId: widget.id, value: String(value) };
  }
  const dims = opts.dimensions || [];
  const dim = dims.find((d) => d.column === column);
  if (dim) return { kind: 'filter', token: dim.token, value: String(value) };
  return null;
}
