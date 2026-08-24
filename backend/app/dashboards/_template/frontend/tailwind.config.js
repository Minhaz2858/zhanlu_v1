/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Design-token-driven aliases. The actual values are injected at runtime
        // as CSS custom properties by useDesignTokens.js from config.design;
        // when a dashboard has no design system, the :root defaults in
        // index.css apply (built-in slate palette — backward compatible).
        primary: 'rgb(var(--ds-primary-rgb) / <alpha-value>)',
        'primary-foreground': 'rgb(var(--ds-on-primary-rgb) / <alpha-value>)',
        secondary: 'rgb(var(--ds-secondary-rgb) / <alpha-value>)',
        accent: 'rgb(var(--ds-accent-rgb) / <alpha-value>)',
        background: 'rgb(var(--ds-background-rgb) / <alpha-value>)',
        foreground: 'rgb(var(--ds-foreground-rgb) / <alpha-value>)',
        muted: 'rgb(var(--ds-muted-rgb) / <alpha-value>)',
        border: 'rgb(var(--ds-border-rgb) / <alpha-value>)',
        destructive: 'rgb(var(--ds-destructive-rgb) / <alpha-value>)',
        ring: 'rgb(var(--ds-ring-rgb) / <alpha-value>)',
        // Chart palette: chart-1..chart-6.
        chart: {
          1: 'rgb(var(--ds-chart-1-rgb) / <alpha-value>)',
          2: 'rgb(var(--ds-chart-2-rgb) / <alpha-value>)',
          3: 'rgb(var(--ds-chart-3-rgb) / <alpha-value>)',
          4: 'rgb(var(--ds-chart-4-rgb) / <alpha-value>)',
          5: 'rgb(var(--ds-chart-5-rgb) / <alpha-value>)',
          6: 'rgb(var(--ds-chart-6-rgb) / <alpha-value>)',
        },
      },
      fontFamily: {
        heading: ['var(--ds-font-heading)', 'sans-serif'],
        body: ['var(--ds-font-body)', 'sans-serif'],
      },
      borderRadius: {
        card: 'var(--ds-card-radius)',
      },
    },
  },
  plugins: [],
};
