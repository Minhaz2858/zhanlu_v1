import { useState, useEffect } from 'react';
import { authFetch } from '@/api/authFetch';
import { Loader2, X, Users, Search, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * ResourceAccessDialog — admin-only dialog for managing per-user access
 * to a specific Project or AgentApp.
 *
 * Props:
 *   resourceType: 'project' | 'agent'
 *   resourceId:   string (UUID)
 *   resourceName: string
 *   onClose:      () => void
 *
 * Lists ALL users with checkboxes.  Checked = currently shared with.
 * Toggling a user immediately POSTs (grant) or DELETEs (revoke) the
 * corresponding ResourceShare row.
 */
export default function ResourceAccessDialog({
  resourceType,
  resourceId,
  resourceName,
  onClose,
  onConfigureAccess,
}) {
  const [users, setUsers] = useState([]);
  const [sharedUserIds, setSharedUserIds] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [pendingIds, setPendingIds] = useState(new Set()); // mid-flight toggles

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        // Fetch all users + current shares for this resource in parallel
        const [usersResp, sharesResp] = await Promise.all([
          authFetch('/api/admin/users'),
          authFetch(`/api/shares?resource_type=${resourceType}&resource_id=${resourceId}`),
        ]);

        if (usersResp.ok) {
          const data = await usersResp.json();
          setUsers(data);
        } else {
          const e = await usersResp.json().catch(() => ({}));
          throw new Error(e.detail || 'Failed to load users');
        }

        if (sharesResp.ok) {
          const sharesData = await sharesResp.json();
          // Collect shared_with_user_id from the `given` list (admin grants)
          const ids = new Set();
          for (const s of (sharesData.given || [])) {
            if (s.shared_with_user_id) ids.add(s.shared_with_user_id);
          }
          setSharedUserIds(ids);
        }
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [resourceType, resourceId]);

  async function toggleUser(userId) {
    if (pendingIds.has(userId)) return; // already in flight

    const currentlyShared = sharedUserIds.has(userId);
    setPendingIds((prev) => new Set(prev).add(userId));

    try {
      if (currentlyShared) {
        // Revoke: find the share id from the user id, then DELETE
        const sharesResp = await authFetch(
          `/api/shares?resource_type=${resourceType}&resource_id=${resourceId}`
        );
        if (sharesResp.ok) {
          const data = await sharesResp.json();
          const share = (data.given || []).find(
            (s) => s.shared_with_user_id === userId
          );
          if (share) {
            const delResp = await authFetch(`/api/shares/${share.id}`, {
              method: 'DELETE',
            });
            if (delResp.ok) {
              setSharedUserIds((prev) => {
                const next = new Set(prev);
                next.delete(userId);
                return next;
              });
            } else {
              const e = await delResp.json().catch(() => ({}));
              throw new Error(e.detail || 'Failed to revoke access');
            }
          }
        }
      } else {
        // Grant
        const resp = await authFetch('/api/shares', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            resource_type: resourceType,
            resource_id: resourceId,
            shared_with_user_id: userId,
            access_level: 'use',
          }),
        });
        if (resp.ok) {
          setSharedUserIds((prev) => new Set(prev).add(userId));
        } else {
          const e = await resp.json().catch(() => ({}));
          throw new Error(e.detail || 'Failed to grant access');
        }
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setPendingIds((prev) => {
        const next = new Set(prev);
        next.delete(userId);
        return next;
      });
    }
  }

  const filtered = users.filter(
    (u) =>
      !search ||
      u.email.toLowerCase().includes(search.toLowerCase()) ||
      (u.full_name || '').toLowerCase().includes(search.toLowerCase())
  );

  const typeLabel = resourceType === 'project' ? 'Project' : 'Agent';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-lg text-foreground">
            <Users className="h-5 w-5 text-primary" />
            Manage Access
          </h2>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <p className="mb-3 text-sm text-muted-foreground">
          Toggle users who can access{' '}
          <span className="font-medium text-foreground">
            {resourceName || `this ${typeLabel.toLowerCase()}`}
          </span>
        </p>

        {loading ? (
          <div className="flex justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            <div className="relative mb-2">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <input
                placeholder="Search users..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full rounded-lg border border-border bg-background py-2 pl-8 pr-3 text-sm"
              />
            </div>

            <div className="max-h-64 overflow-y-auto rounded-lg border border-border">
              {filtered.length === 0 ? (
                <p className="px-3 py-4 text-center text-sm text-muted-foreground">
                  No users found.
                </p>
              ) : (
                filtered.map((u) => {
                  const checked = sharedUserIds.has(u.id);
                  const pending = pendingIds.has(u.id);
                  return (
                    <label
                      key={u.id}
                      className={`flex cursor-pointer items-center gap-3 border-b border-border px-3 py-2.5 last:border-0 hover:bg-muted/30 transition-colors ${
                        checked ? 'bg-primary/5' : ''
                      }`}
                    >
                      <span className="relative flex h-5 w-5 shrink-0 items-center justify-center">
                        {pending ? (
                          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                        ) : (
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleUser(u.id)}
                            disabled={pending}
                            className="h-4 w-4 rounded accent-primary"
                          />
                        )}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-foreground">
                          {u.full_name || '—'}
                        </p>
                        <p className="truncate text-xs text-muted-foreground">
                          {u.email}
                        </p>
                      </div>
                      {u.role === 'admin' && (
                        <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                          admin
                        </span>
                      )}
                      {checked && onConfigureAccess && (
                        <button
                          type="button"
                          title="Configure data access"
                          onClick={(e) => {
                            e.stopPropagation();
                            e.preventDefault();
                            onConfigureAccess(u.id);
                          }}
                          className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                        >
                          <ShieldCheck className="h-4 w-4" />
                        </button>
                      )}
                    </label>
                  );
                })
              )}
            </div>
          </>
        )}

        {error && (
          <p className="mt-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </p>
        )}

        <div className="mt-4 flex items-center gap-2 rounded-lg bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <Users className="h-3.5 w-3.5 shrink-0" />
          Checked users can view + use this resource. Uncheck to revoke
          access immediately.
        </div>

        <div className="mt-3 flex justify-end">
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}
