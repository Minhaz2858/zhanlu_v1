/**
 * ProductSummaryCard — compact "at-a-glance" card for one chemical product.
 *
 * Shows: product name (top), large price, a "Price %" chip, a "7d sales %"
 * chip, and a `High <H> Low <L> • 7d sales <N>t` footer — mirroring the
 * upstream/midstream/downstream sidebar cards in the market overview.
 *
 * Direction is conveyed via chip color + arrow glyph. Price and sales use
 * opposite color semantics (in the CJK commodity convention price up = red,
 * but sales down = red because a drop in volume is bad).
 */

const NEUTRAL_CHIP = 'bg-muted text-muted-foreground border-border';

// Price chip: up = rose/red, down = emerald/green, zero = neutral gray.
const PRICE_UP = 'bg-rose-50 text-rose-700 border-rose-100 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20';
const PRICE_DOWN = 'bg-emerald-50 text-emerald-700 border-emerald-100 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20';

// Sales chip: up = blue, down = rose/red (inverted — falling sales is bad).
const SALES_UP = 'bg-blue-50 text-blue-700 border-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:border-blue-500/20';
const SALES_DOWN = 'bg-rose-50 text-rose-700 border-rose-100 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20';

const BASE_CHIP = 'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-none';

function formatNumber(value, maxFractionDigits = 2) {
  if (value == null || !Number.isFinite(value)) return null;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxFractionDigits,
  });
}

/**
 * Build a chip descriptor for a percentage change.
 *
 * @param {number|null|undefined} value  percentage change value
 * @param {'price'|'sales'} kind         which color semantics to apply
 * @returns {{ arrow: string, label: string, classes: string }}
 */
export function formatChangeChip(value, kind = 'price') {
  const num = value == null || !Number.isFinite(value) ? null : value;

  if (num == null) {
    return { arrow: '', label: '--', classes: NEUTRAL_CHIP };
  }

  const dir = num > 0 ? 1 : num < 0 ? -1 : 0;
  const arrow = dir > 0 ? '▲' : dir < 0 ? '▼' : '';
  const label = `${num > 0 ? '+' : ''}${num.toFixed(2)}%`;

  let classes = NEUTRAL_CHIP;
  if (kind === 'sales') {
    classes = dir > 0 ? SALES_UP : dir < 0 ? SALES_DOWN : NEUTRAL_CHIP;
  } else {
    classes = dir > 0 ? PRICE_UP : dir < 0 ? PRICE_DOWN : NEUTRAL_CHIP;
  }

  return { arrow, label, classes };
}

/** Price-change chip (up = red, down = green). */
export function formatPriceChip(value) {
  return formatChangeChip(value, 'price');
}

/** Sales-change chip (up = blue, down = red — inverted semantics). */
export function formatSalesChip(value) {
  return formatChangeChip(value, 'sales');
}

export default function ProductSummaryCard({
  name,
  price,
  priceChangePct,
  sales7dChangePct,
  high,
  low,
  sales7dTons,
}) {
  const priceChip = formatPriceChip(priceChangePct);
  const salesChip = formatSalesChip(sales7dChangePct);

  const priceText = formatNumber(price);
  const highText = formatNumber(high);
  const lowText = formatNumber(low);
  const salesText = formatNumber(sales7dTons, 1);

  return (
    <article className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3.5">
      <div className="text-[10px] md:text-[11px] font-medium uppercase tracking-[0.06em] text-muted-foreground truncate">
        {name || '--'}
      </div>

      <div className="mt-1.5 font-mono text-2xl font-semibold text-foreground tabular-nums">
        {priceText != null ? priceText : '--'}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className={`${BASE_CHIP} ${priceChip.classes}`}>
          {priceChip.arrow && <span aria-hidden="true">{priceChip.arrow}</span>}
          <span>{priceChip.label}</span>
        </span>
        <span className={`${BASE_CHIP} ${salesChip.classes}`}>
          {salesChip.arrow && <span aria-hidden="true">{salesChip.arrow}</span>}
          <span>{salesChip.label}</span>
        </span>
      </div>

      <div className="mt-3 border-t border-slate-100 dark:border-slate-800 pt-2 font-mono text-[11px] md:text-[12px] text-muted-foreground tabular-nums">
        <span>High {highText != null ? highText : '--'}</span>
        <span className="mx-1.5">Low {lowText != null ? lowText : '--'}</span>
        <span>• 7d sales {salesText != null ? `${salesText}t` : '--'}</span>
      </div>
    </article>
  );
}
