import { useNavigate } from 'react-router-dom';
import { Workflow, Store, Wrench, Settings, X, Building2 } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * MoreSheet — bottom sheet listing the desktop pages that don't fit in
 * the two main bottom tabs (聊天 / 我的空间). Tapping an item navigates
 * there and closes the sheet.
 */
export default function MoreSheet({ open, onClose }) {
  const { t } = useLanguage();
  const navigate = useNavigate();

  if (!open) return null;

  const items = [
    { to: '/from-company', label: t.sidebar.fromCompany, icon: Building2 },
    { to: '/automation', label: t.sidebar.automation, icon: Workflow },
    { to: '/market', label: t.sidebar.market, icon: Store },
    { to: '/toolkit', label: t.sidebar.toolkit, icon: Wrench },
    { to: '/settings', label: t.common.settings, icon: Settings },
  ];

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="absolute inset-x-0 bottom-0 rounded-t-2xl bg-background pb-[calc(env(safe-area-inset-bottom)+0.75rem)] shadow-2xl">
        <div className="mx-auto mt-2.5 h-1 w-10 rounded-full bg-border" />
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <span className="font-display text-base text-foreground">{t.mobile.moreTitle}</span>
          <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-secondary">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid grid-cols-4 gap-2 px-4 py-4">
          {items.map((it) => (
            <button
              key={it.to}
              onClick={() => { onClose(); navigate(it.to); }}
              className="flex flex-col items-center gap-1.5 rounded-xl px-2 py-3 text-xs text-foreground transition-colors hover:bg-secondary"
            >
              <it.icon className="h-5 w-5 text-primary" />
              {it.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
