import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';

export default function PageHeader({ title, subtitle, action, backTo }) {
  const navigate = useNavigate();
  return (
    <div className="mb-8 flex items-start justify-between gap-4">
      <div className="flex items-start gap-3">
        <button
          onClick={() => { if (window.history.length > 1) navigate(-1); else if (backTo) navigate(backTo); else navigate('/'); }}
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