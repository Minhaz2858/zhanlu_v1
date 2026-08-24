/**
 * Applies the design-system tokens from config.design as CSS custom properties
 * on :root. Every visual element (backgrounds, text, borders, chart colors,
 * fonts) is driven by these variables; when config.design is absent we leave
 * the built-in slate defaults in place (backward compatible).
 *
 * Exported helpers are also used by widgets/ to read the active chart palette
 * without reaching into the DOM on every render.
 */

export const DESIGN_DEFAULTS = {
  colors: {
    primary: '#2563eb',
    on_primary: '#ffffff',
    secondary: '#64748b',
    accent: '#f59e0b',
    background: '#f8fafc',
    foreground: '#0f172a',
    muted: '#f1f5f9',
    border: '#e2e8f0',
    destructive: '#ef4444',
    ring: '#2563eb',
    chart_palette: ['#2563eb', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'],
    dark: {
      background: '#020617',
      foreground: '#f8fafc',
      muted: '#1e293b',
      border: '#334155',
    },
  },
  typography: {
    heading: 'Inter',
    body: 'Inter',
    google_fonts_url: '',
    css_import: '',
  },
  spacing: {
    xs: '2px', sm: '4px', md: '8px', lg: '12px', xl: '16px', '2xl': '24px', '3xl': '32px',
  },
  style: { name: 'Enterprise Clean', keywords: '', card_radius: '8px' },
};

/** Merge the runtime design over the defaults so partial configs still work. */
function mergeDesign(runtime) {
  if (!runtime || typeof runtime !== 'object') return DESIGN_DEFAULTS;
  const base = structuredClone(DESIGN_DEFAULTS);
  if (runtime.colors) base.colors = { ...base.colors, ...runtime.colors };
  if (runtime.typography) base.typography = { ...base.typography, ...runtime.typography };
  if (runtime.spacing) base.spacing = { ...base.spacing, ...runtime.spacing };
  if (runtime.style) base.style = { ...base.style, ...runtime.style };
  return base;
}

let activeDesign = DESIGN_DEFAULTS;

/** Reads the currently applied design tokens (for widgets/charts). */
export function getDesign() {
  return activeDesign;
}

/** Chart palette used by line/bar/pie/radar widgets (no DOM reads). */
export function getChartPalette() {
  return activeDesign.colors.chart_palette || DESIGN_DEFAULTS.colors.chart_palette;
}

/** Load a Google Fonts stylesheet if the design system provides one. */
function loadFonts(design) {
  const url = design.typography?.google_fonts_url;
  if (!url) return;
  const existing = document.getElementById('design-fonts');
  if (existing) existing.remove();
  const link = document.createElement('link');
  link.id = 'design-fonts';
  link.rel = 'stylesheet';
  link.href = url;
  document.head.appendChild(link);
}

/** #rrggbb -> "r g b" so Tailwind's rgb(var(--x) / alpha) resolves. */
function rgbTriplet(hex) {
  if (!hex || !/^#[0-9a-fA-F]{6}$/.test(hex)) return null;
  const n = parseInt(hex.slice(1), 16);
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`;
}

/**
 * Apply design tokens as CSS custom properties on :root (light) and :root.dark.
 * Must be called once after config.json is fetched. Returns the merged design.
 *
 * Every color is emitted BOTH as a hex var (--ds-primary) for direct CSS use
 * and as an rgb triplet (--ds-primary-rgb) for Tailwind utilities compiled with
 * `rgb(var(--ds-primary-rgb) / <alpha-value>)`.
 */
export function applyDesignTokens(runtimeDesign) {
  const design = mergeDesign(runtimeDesign);
  activeDesign = design;
  const root = document.documentElement;
  const c = design.colors;
  const typo = design.typography;

  const colorKeys = [
    'primary',
    'on-primary',
    'secondary',
    'accent',
    'background',
    'foreground',
    'muted',
    'border',
    'destructive',
    'ring',
  ];
  const vars = {
    '--ds-card-radius': design.style?.card_radius || '8px',
    '--ds-font-heading': `'${typo.heading || 'Inter'}', sans-serif`,
    '--ds-font-body': `'${typo.body || 'Inter'}', sans-serif`,
  };
  colorKeys.forEach((k) => {
    const hex = c[k.replace('-', '_')] || c[k] || '';
    if (hex) {
      vars[`--ds-${k}`] = hex;
      const rgb = rgbTriplet(hex);
      if (rgb) vars[`--ds-${k}-rgb`] = rgb;
    }
  });
  (c.chart_palette || []).forEach((hex, i) => {
    vars[`--ds-chart-${i + 1}`] = hex;
    const rgb = rgbTriplet(hex);
    if (rgb) vars[`--ds-chart-${i + 1}-rgb`] = rgb;
  });

  Object.entries(vars).forEach(([k, v]) => root.style.setProperty(k, v));

  // Dark-mode overrides.
  const dark = c.dark || {};
  const darkVars = {
    '--ds-background-dark': dark.background || '#020617',
    '--ds-foreground-dark': dark.foreground || '#f8fafc',
    '--ds-muted-dark': dark.muted || '#1e293b',
    '--ds-border-dark': dark.border || '#334155',
  };
  Object.entries(darkVars).forEach(([k, v]) => root.style.setProperty(k, v));

  // rgb triplets for the dark palette too.
  const darkRgb = {
    '--ds-background-dark-rgb': rgbTriplet(dark.background),
    '--ds-foreground-dark-rgb': rgbTriplet(dark.foreground),
    '--ds-muted-dark-rgb': rgbTriplet(dark.muted),
    '--ds-border-dark-rgb': rgbTriplet(dark.border),
  };
  Object.entries(darkRgb).forEach(([k, v]) => {
    if (v) root.style.setProperty(k, v);
  });

  loadFonts(design);
  return design;
}
