import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';

export default function PageHeader({ title, subtitle, action, backTo }) {
  const navigate = useNavigate();
  return (
    <div className="mb-8 flex items-start justify-between gap-4">
      <div className="flex items-start gap-3">
        <button
          // Prefer the page's DECLARED parent over history. Detail routes here
          // (/my-space/agent/:id, /my-space/project/:id) unmount the tabbed list
          // page, and history is the wrong answer: reloading a detail URL leaves
          // a non-tab entry behind you, and an entry from the sidebar/Chat can
          // sit there too — so navigate(-1) lands on a URL carrying no ?tab= and
          // the list reopens on its default tab (2026-09-16). `backTo` is
          // authoritative whenever a page declares one; history is the fallback
          // for pages that don't.
          onClick={() => { if (backTo) navigate(backTo); else if (window.history.length > 1) navigate(-1); else navigate('/'); }}
          className="mt-1 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div>
          <h1 className="font-display text-3xl tracking-tight text-foreground">{title}</h1>
          {subtitle && <p className="mt-2 text-sm text-muted-foreground">{subtitle}</p>}
        </div>
      </div>
      {action}
    </div>
  );
}