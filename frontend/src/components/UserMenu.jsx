import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/lib/AuthContext';
import { useLanguage } from '@/lib/LanguageProvider';
import {
  Settings, Globe, HelpCircle, ChevronRight, LogOut, LogIn,
  LayoutGrid, Download, Info, ChevronUp, Check, X,
  ChevronDown, Sun, Moon, Monitor,
} from 'lucide-react';
import { useTheme } from '@/lib/useTheme';

export default function UserMenu({ user, collapsed = false }) {
  const { t, lang, setLang } = useLanguage();
  const { theme, setTheme } = useTheme();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [langOpen, setLangOpen] = useState(false);
  const [themeOpen, setThemeOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e) { if (ref.current && !ref.current.contains(e.target)) { setOpen(false); setLangOpen(false); setThemeOpen(false); } }
    function onEsc(e) { if (e.key === 'Escape') { setOpen(false); setLangOpen(false); setThemeOpen(false); } }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onEsc); };
  }, [open]);

  const name = user?.full_name || user?.email?.split('@')[0] || t.common.guest;
  const initial = name.charAt(0).toUpperCase();

  async function handleLogout() {
    setOpen(false);
    logout();
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex w-full items-center rounded-lg px-2 py-2 transition-colors hover:bg-sidebar-accent/50 ${collapsed ? 'justify-center' : 'gap-2.5'}`}
      >
        <div className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-medium text-primary-foreground">
          {initial}
        </div>
        {!collapsed && (
          <>
            <div className="min-w-0 flex-1 text-left">
              <div className="truncate text-sm text-foreground">{name}</div>
              <div className="truncate text-[11px] text-muted-foreground">{user?.role === 'admin' ? t.common.admin : t.common.user}</div>
            </div>
            <ChevronUp className={`h-4 w-4 text-muted-foreground transition-transform ${open ? '' : 'rotate-180'}`} />
          </>
        )}
      </button>

      {open && (
        <div className={`absolute bottom-full z-50 mb-2 overflow-hidden rounded-xl border border-border bg-popover shadow-lg ${collapsed ? 'left-0 w-60' : 'left-0 right-0'}`}>
          <div className="border-b border-border px-3 py-2.5">
            <div className="truncate text-xs text-muted-foreground">{user?.email || name}</div>
          </div>

          <div className="py-1">
            <MenuItem icon={Settings} label={t.common.settings} shortcut="⇧⌘," onClick={() => { setOpen(false); navigate('/settings'); }} />
            {/* UI Language */}
            <MenuItem icon={Globe} label={t.userMenu.language} trailing={<ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />} onClick={() => setLangOpen((v) => !v)} />
            {langOpen && (
              <div className="ml-7 pb-1">
                <LangOption label={t.userMenu.zh} active={lang === 'zh'} onClick={() => { setLang('zh'); setLangOpen(false); }} />
                <LangOption label={t.userMenu.en} active={lang === 'en'} onClick={() => { setLang('en'); setLangOpen(false); }} />
              </div>
            )}
            <MenuItem icon={theme === 'dark' ? Moon : theme === 'light' ? Sun : Monitor} label={t.userMenu.theme} trailing={<ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />} onClick={() => setThemeOpen((v) => !v)} />
            {themeOpen && (
              <div className="ml-7 pb-1">
                <LangOption icon={Sun} label={t.userMenu.light} active={theme === 'light'} onClick={() => { setTheme('light'); setThemeOpen(false); }} />
                <LangOption icon={Moon} label={t.userMenu.dark} active={theme === 'dark'} onClick={() => { setTheme('dark'); setThemeOpen(false); }} />
                <LangOption icon={Monitor} label={t.userMenu.system} active={theme === 'system'} onClick={() => { setTheme('system'); setThemeOpen(false); }} />
              </div>
            )}
            <MenuItem icon={HelpCircle} label={t.userMenu.help} onClick={() => setOpen(false)} />
          </div>

          <div className="border-t border-border py-1">
            <MenuItem icon={LayoutGrid} label={t.userMenu.plans} onClick={() => setOpen(false)} />
            <MenuItem icon={Download} label={t.userMenu.apps} onClick={() => setOpen(false)} />
            <MenuItem icon={Info} label={t.userMenu.learnMore} trailing={<ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />} onClick={() => setOpen(false)} />
          </div>

          <div className="border-t border-border py-1">
            {user ? (
              <MenuItem icon={LogOut} label={t.userMenu.logout} onClick={handleLogout} danger />
            ) : (
              // Guest: offer a clear path to the login page so they can
              // authenticate (the app is public for browsing, but writes
              // require a real login — surfaced as "Authentication required").
              <MenuItem
                icon={LogIn}
                label={lang === 'en' ? 'Log in' : '登录'}
                onClick={() => { setOpen(false); navigate('/login'); }}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MenuItem({ icon: Icon, label, shortcut, trailing, onClick, danger }) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors hover:bg-secondary/70 ${danger ? 'text-destructive' : 'text-foreground'}`}
    >
      <Icon className={`h-4 w-4 ${danger ? 'text-destructive' : 'text-muted-foreground'}`} />
      <span className="flex-1">{label}</span>
      {shortcut && <span className="text-xs text-muted-foreground">{shortcut}</span>}
      {trailing}
    </button>
  );
}

function LangOption({ label, active, onClick, icon: Icon }) {
  return (
    <button onClick={onClick} className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
      {Icon && <Icon className="h-3.5 w-3.5 text-muted-foreground" />}
      <span className="flex-1">{label}</span>
      {active ? <Check className="h-3.5 w-3.5 text-primary" /> : <X className="h-3.5 w-3.5 text-transparent" />}
    </button>
  );
}