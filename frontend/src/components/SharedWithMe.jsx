import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/api/authFetch';
import { formatDate } from '@/lib/time';
import { Loader2, Share2, FolderKanban, Bot, Trash2 } from 'lucide-react';

/**
 * SharedWithMe — grid of resources shared with the current user.
 * Renders inside MySpace as a tab. Each card links to the resource
 * detail page. Shared resources are view-only (can_edit=false).
 */
export default function SharedWithMe() {
  const [shares, setShares] = useState([]);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch('/api/shares');
      if (!resp.ok) return;
      const data = await resp.json();
      setShares(data.received || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  }

  if (shares.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
        <Share2 className="mb-3 h-8 w-8 text-muted-foreground/40" />
        <p className="text-sm text-muted-foreground">No resources have been shared with you yet.</p>
        <p className="mt-1 text-xs text-muted-foreground">When someone shares a project or agent, it will appear here.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <Share2 className="h-4 w-4 text-primary" />
        <p className="text-sm text-muted-foreground">{shares.length} resource(s) shared with you · view only</p>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {shares.map((s) => {
          const Icon = s.resource_type === 'project' ? FolderKanban : Bot;
          const detailPath = s.resource_type === 'project'
            ? `/my-space/project/${s.resource_id}`
            : `/my-space/agent/${s.resource_id}`;
          return (
            <div
              key={s.id}
              onClick={() => navigate(detailPath)}
              className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm"
            >
              <div className="mb-2 flex items-start gap-2">
                <Icon className="mt-0.5 h-4 w-4 text-primary" />
                <h3 className="flex-1 font-display text-base text-foreground group-hover:text-primary">
                  {s.resource_type === 'project' ? 'Shared Project' : 'Shared Agent'}
                </h3>
                <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-300">
                  view only
                </span>
              </div>
              <p className="mb-3 flex-1 text-xs text-muted-foreground">
                Shared by {s.shared_by_name || s.shared_by_email || 'another user'}
              </p>
              <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                <span className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground">
                  {s.resource_type}
                </span>
                <span className="text-xs text-muted-foreground">
                  {s.created_date ? formatDate(s.created_date) : ''}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
