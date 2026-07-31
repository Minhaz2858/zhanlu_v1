// Ported from EDIA_5.1.2/frontend/src/lib/chartTheme.ts
export function getChartTheme(isDark) {
  if (isDark) {
    return {
      grid: '#2a2f3a',
      axis: '#4b5563',
      tick: '#909296',
      tickMuted: '#6b7280',
      tooltip: {
        backgroundColor: '#1c1f24',
        border: '1px solid #373a40',
        color: '#c1c2c5',
      },
      brushFill: '#25262b',
      brushStroke: '#4b5563',
    };
  }
  return {
    grid: '#e2e8f0',
    axis: '#cbd5e1',
    tick: '#64748b',
    tickMuted: '#94a3b8',
    tooltip: {
      backgroundColor: '#ffffff',
      border: '1px solid #e2e8f0',
      color: '#64748b',
    },
    brushFill: '#f8fafc',
    brushStroke: '#cbd5e1',
  };
}

export function rechartsTooltipStyle(theme, isDark = false) {
  return {
    backgroundColor: theme.tooltip.backgroundColor,
    border: theme.tooltip.border,
    color: theme.tooltip.color,
    borderRadius: isDark ? 10 : 8,
    fontSize: isDark ? 12 : 11,
    boxShadow: isDark
      ? '0 10px 24px rgba(0, 0, 0, 0.35)'
      : '0 4px 6px -1px rgb(0 0 0 / 0.08)',
  };
}
