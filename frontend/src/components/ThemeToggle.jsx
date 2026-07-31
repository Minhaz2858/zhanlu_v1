import { Sun, Moon, Monitor } from 'lucide-react';
import { useTheme } from '@/lib/useTheme';

// Cycles light -> dark -> system -> light. The useTheme hook already applies
// the .dark class to <html> and persists the choice; this component only
// surfaces it. `collapsed` renders an icon-only button matching the sidebar.
const ORDER = ['light', 'dark', 'system'];
const ICON = { light: Sun, dark: Moon, system: Monitor };
const LABEL = { light: 'Light', dark: 'Dark', system: 'System' };

export default function ThemeToggle({ collapsed = false }) {
  const { theme, setTheme } = useTheme();
  const current = ORDER.includes(theme) ? theme : 'system';
  const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
  const Icon = ICON[current];
  return (
    <button
      onClick={() => setTheme(next)}
      aria-label={`Theme: ${LABEL[current]}. Click for ${LABEL[next]}`}
      title={`Theme: ${LABEL[current]}`}
      className={`flex items-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground ${
        collapsed ? 'justify-center p-2' : 'gap-2 px-2 py-1.5 text-xs'
      }`}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {!collapsed && <span>{LABEL[current]}</span>}
    </button>
  );
}
