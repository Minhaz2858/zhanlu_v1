# Style Recipes

The same design can be rendered in 4 distinct visual styles by adjusting corner radius (`rectRadius`) and spacing. Choose the style recipe that fits the presentation tone.

> **Unit note**: PptxGenJS uses inches. Slide dimensions are 10" × 5.625" (LAYOUT_16x9) or 13.3" × 7.5" (LAYOUT_WIDE).

## Style Overview

| Style | Corner Radius | Spacing | Best For |
|-------|--------------|---------|----------|
| **Sharp & Compact** | 0 ~ 0.05" | Tight | Data-dense, tables, professional reports |
| **Soft & Balanced** | 0.08" ~ 0.12" | Moderate | Corporate, business presentations, general use |
| **Rounded & Spacious** | 0.15" ~ 0.25" | Relaxed | Product intros, marketing, creative showcases |
| **Pill & Airy** | 0.3" ~ 0.5" | Open | Brand showcases, launch events, premium presentations |

---

## Sharp & Compact

**Visual character**: Geometric, high information density, formal and serious.

| Category | Value (inches) | Notes |
|----------|---------------|-------|
| Corner radius — small | 0" | Full right angle |
| Corner radius — medium | 0.03" | Micro-rounded |
| Corner radius — large | 0.05" | Slight rounding |
| Element padding | 0.1" ~ 0.15" | Compact |
| Element gap | 0.1" ~ 0.2" | Compact |
| Page margin | 0.3" | Narrow |
| Block gap | 0.25" ~ 0.35" | Compact |

```javascript
// Sharp style card
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 0.5, y: 1, w: 4, h: 2.5,
  fill: { color: theme.bg },
  rectRadius: 0.03
});
```

---

## Soft & Balanced

**Visual character**: Moderate rounding, comfortable whitespace, professional yet approachable.

| Category | Value (inches) | Notes |
|----------|---------------|-------|
| Corner radius — small | 0.05" | Slight rounding |
| Corner radius — medium | 0.08" | Medium rounding |
| Corner radius — large | 0.12" | Larger rounding |
| Element padding | 0.15" ~ 0.2" | Moderate |
| Element gap | 0.15" ~ 0.25" | Moderate |
| Page margin | 0.4" | Standard |
| Block gap | 0.35" ~ 0.5" | Moderate |

```javascript
// Soft style card
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 0.5, y: 1, w: 4, h: 2.5,
  fill: { color: theme.bg },
  rectRadius: 0.1
});
```

---

## Rounded & Spacious

**Visual character**: Large corners, generous whitespace, friendly and modern.

| Category | Value (inches) | Notes |
|----------|---------------|-------|
| Corner radius — small | 0.1" | Medium rounding |
| Corner radius — medium | 0.15" | Large rounding |
| Corner radius — large | 0.25" | Very large rounding |
| Element padding | 0.2" ~ 0.3" | Relaxed |
| Element gap | 0.25" ~ 0.4" | Relaxed |
| Page margin | 0.5" | Wide |
| Block gap | 0.5" ~ 0.7" | Relaxed |

```javascript
// Rounded style card
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 0.5, y: 1, w: 4, h: 2.5,
  fill: { color: theme.bg },
  rectRadius: 0.2
});
```

---

## Pill & Airy

**Visual character**: Full pill-shaped corners, abundant whitespace, light and open feel, strong brand presence.

| Category | Value (inches) | Notes |
|----------|---------------|-------|
| Corner radius — small | 0.2" | Large rounding |
| Corner radius — medium | 0.3" | Pill shape |
| Corner radius — large | 0.5" | Full pill |
| Element padding | 0.25" ~ 0.4" | Open |
| Element gap | 0.3" ~ 0.5" | Open |
| Page margin | 0.6" | Wide |
| Block gap | 0.6" ~ 0.9" | Open |

```javascript
// Pill style button (height 0.4", rectRadius 0.2" = perfect pill)
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 3, y: 4, w: 2, h: 0.4,
  fill: { color: theme.primary },
  rectRadius: 0.2
});
```

---

## Component Style Mapping

| Component | Sharp | Soft | Rounded | Pill |
|-----------|-------|------|---------|------|
| **Button / Tag** | rectRadius: 0 | rectRadius: 0.05 | rectRadius: 0.1 | rectRadius: 0.2 |
| **Card / Container** | rectRadius: 0.03 | rectRadius: 0.1 | rectRadius: 0.2 | rectRadius: 0.3 |
| **Image Container** | rectRadius: 0 | rectRadius: 0.08 | rectRadius: 0.15 | rectRadius: 0.25 |
| **Input Field** | rectRadius: 0 | rectRadius: 0.05 | rectRadius: 0.1 | rectRadius: 0.2 |
| **Badge** | rectRadius: 0.02 | rectRadius: 0.05 | rectRadius: 0.08 | rectRadius: 0.15 |
| **Avatar Frame** | rectRadius: 0 | rectRadius: 0.1 | rectRadius: 0.2 | rectRadius: 0.5 (circle) |

---

## Mixing Rules

### 1. Outer container corner >= inner element corner

```javascript
// ✅ Correct: outer > inner
card:   rectRadius: 0.2
button: rectRadius: 0.1

// ❌ Wrong: inner > outer → visual overflow effect
card:   rectRadius: 0.1
button: rectRadius: 0.2
```

### 2. Information density drives spacing

| Zone Type | Recommended Style |
|-----------|------------------|
| Data display zone | Sharp / Soft (compact spacing) |
| Content browsing zone | Rounded / Pill (relaxed spacing) |
| Title zone | Soft / Rounded (moderate spacing) |

### 3. Corner radius vs element height

| Element Height | Sharp | Soft | Rounded | Pill |
|---------------|-------|------|---------|------|
| Small (< 0.3") | 0" | 0.03" | 0.08" | height/2 |
| Medium (0.3" ~ 0.6") | 0.02" | 0.05" | 0.12" | height/2 |
| Large (0.6" ~ 1.2") | 0.03" | 0.08" | 0.2" | 0.3" |
| Extra large (> 1.2") | 0.05" | 0.12" | 0.25" | 0.4" |

> **Pill tip**: For a perfect pill shape, set `rectRadius = element height / 2`

---

## Quick Selection Guide

| Presentation Type | Recommended Style | Reason |
|------------------|------------------|--------|
| Finance / Data reports | Sharp & Compact | High density, serious and precise |
| Corporate / Business | Soft & Balanced | Balances professionalism and approachability |
| Product intro / Marketing | Rounded & Spacious | Modern feel, friendly |
| Launch events / Brand | Pill & Airy | Premium feel, visual impact |
| Training / Education | Soft / Rounded | Clear, readable, friendly |
| Tech sharing | Sharp / Soft | Professional, information-dense |

---

## Critical PptxGenJS Notes

- **`rectRadius` only works on `ROUNDED_RECTANGLE`** — not `RECTANGLE`. Using it on `RECTANGLE` is silently ignored.
- **Don't pair `ROUNDED_RECTANGLE` with rectangular accent overlays** — they won't cover rounded corners. Use `RECTANGLE` instead when you need accent bars.
- **Never reuse option objects across calls** — PptxGenJS mutates objects in-place. Use a factory function:
  ```javascript
  const makeCardStyle = () => ({ rectRadius: 0.1, fill: { color: theme.bg } });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { ...makeCardStyle(), x: 0.5, y: 1, w: 4, h: 2.5 });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { ...makeCardStyle(), x: 5, y: 1, w: 4, h: 2.5 });
  ```
