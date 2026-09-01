import { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { MessageSquare, Boxes, Folder, Grid3x3, User } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import MoreSheet from './MoreSheet';

/**
 * BottomTabBar — fixed bottom navigation for the mobile layout.
 *
 * Two primary tabs (聊天 / 我的空间) plus a "更多" entry that opens the
 * MoreSheet with the remaining desktop pages. Respects the iOS/Android
 * safe-area inset via env(safe-area-inset-bottom).
 */
export default function BottomTabBar() {
  const { t } = useLanguage();
  const location = useLocation();
  const [moreOpen, setMoreOpen] = useState(false);

  const tabs = [
    { to: '/chat', label: t.mobile.chat, icon: MessageSquare },
    { to: '/from-personal', label: t.mobile.fromPersonal, icon: User },
    { to: '/my-files', label: t.mobile.myFiles, icon: Folder },
  ];

  return (
    <>
      <nav className="flex shrink-0 items-stretch border-t border-border bg-background pb-[env(safe-area-inset-bottom)]">
        {tabs.map((tab) => {
          const active = location.pathname === tab.to;
          return (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-[10px] transition-colors ${
                active ? 'text-primary' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <tab.icon className="h-5 w-5" />
              {tab.label}
            </NavLink>
          );
        })}
        <button
          onClick={() => setMoreOpen(true)}
          className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-[10px] transition-colors ${
            moreOpen ? 'text-primary' : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          <Grid3x3 className="h-5 w-5" />
          {t.mobile.more}
        </button>
      </nav>
      <MoreSheet open={moreOpen} onClose={() => setMoreOpen(false)} />
    </>
  );
}
