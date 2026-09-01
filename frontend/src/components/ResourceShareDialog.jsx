import { useState, useEffect } from 'react';
import { authFetch } from '@/api/authFetch';
import { Loader2, X, Share2, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * ResourceShareDialog — modal for sharing a Project or AgentApp.
 *
 * Props:
 *   resourceType: 'project' | 'agent'
 *   resourceId:   string (UUID)
 *   onClose:      () => void
 *   onShared:     (share) => void  (optional)
 *
 * Fetches all users (requires admin) so the owner can pick a recipient.
 * Non-admins can still share — they type the recipient email manually.
 */
export default function ResourceShareDialog({ resourceType, resourceId, onClose, onShared }) {
  const [users, setUsers] = useState([]);
  const [selectedUserId, setSelectedUserId] = useState('');
  const [manualEmail, setManualEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    async function loadUsers() {
      setLoading(true);
      try {
        const resp = await authFetch('/api/admin/users');
        if (resp.ok) {
          const data = await resp.json();
          setUsers(data);
          setIsAdmin(true);
        }
      } catch {
        // non-admin — fall back to manual email entry
      } finally {
        setLoading(false);
      }
    }
    loadUsers();
  }, []);

  async function share(e) {
    e.preventDefault();
    setSaving(true);
    setError(null);

    let recipientId = selectedUserId;
    if (!recipientId && manualEmail) {
      // Non-admin path: look up user by email via the entity API
      try {
        const resp = await authFetch(`/api/apps/local-zhanlu-app/entities/User?q=${encodeURIComponent(JSON.stringify({ email: manualEmail }))}`);
        if (resp.ok) {
          const data = await resp.json();
          if (Array.isArray(data) && data.length > 0) {
            recipientId = data[0].id;
          }
        }
      } catch { /* fall through */ }
    }

    if (!recipientId) {
      setError('Please select a user or enter a valid email.');
      setSaving(false);
      return;
    }

    try {
      const resp = await authFetch('/api/shares', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          resource_type: resourceType,
          resource_id: resourceId,
          shared_with_user_id: recipientId,
          access_level: 'use',
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'Failed to share');
      if (onShared) onShared(data);
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  const filtered = users.filter((u) =>
    !search || u.email.toLowerCase().includes(search.toLowerCase()) || u.full_name.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-lg" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-lg text-foreground">
            <Share2 className="h-5 w-5 text-primary" />
            Share {resourceType === 'project' ? 'Project' : 'Agent'}
          </h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        <form onSubmit={share} className="space-y-3">
          {loading ? (
            <div className="flex justify-center py-6"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          ) : isAdmin && users.length > 0 ? (
            <>
              <div className="relative">
                <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
                <input
                  placeholder="Search users..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="w-full rounded-lg border border-border bg-background py-2 pl-8 pr-3 text-sm"
                />
              </div>
              <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
                {filtered.map((u) => (
                  <label
                    key={u.id}
                    className={`flex cursor-pointer items-center gap-2 border-b border-border px-3 py-2 last:border-0 hover:bg-muted/30 ${selectedUserId === u.id ? 'bg-primary/5' : ''}`}
                  >
                    <input
                      type="radio"
                      name="recipient"
                      checked={selectedUserId === u.id}
                      onChange={() => setSelectedUserId(u.id)}
                      className="h-4 w-4"
                    />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-foreground">{u.full_name}</p>
                      <p className="truncate text-xs text-muted-foreground">{u.email}</p>
                    </div>
                    {u.role === 'admin' && <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">admin</span>}
                  </label>
                ))}
              </div>
            </>
          ) : (
            <input
              required
              type="email"
              placeholder="Recipient email"
              value={manualEmail}
              onChange={(e) => setManualEmail(e.target.value)}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
            />
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}

          <div className="flex items-center gap-2 rounded-lg bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            <Share2 className="h-3.5 w-3.5 shrink-0" />
            The recipient will get view + use access. They cannot edit or delete this resource.
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" size="sm" disabled={saving || loading}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Share'}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
