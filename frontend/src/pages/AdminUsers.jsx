import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { authFetch } from '@/api/authFetch';
import PageHeader from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Loader2, UserPlus, Trash2, Shield, ShieldCheck, X, Pencil, ChevronDown, Star, Tag } from 'lucide-react';

const APP_ID = import.meta.env.VITE_BASE44_APP_ID || 'local-zhanlu-app';

export default function AdminUsers() {
  const { isAdmin } = useAuth();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingUser, setEditingUser] = useState(null); // user object for edit modal
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await authFetch('/api/admin/users');
      if (!resp.ok) throw new Error('Failed to load users');
      setUsers(await resp.json());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function toggleRole(user) {
    const newRole = user.role === 'admin' ? 'user' : 'admin';
    try {
      const resp = await authFetch(`/api/admin/users/${user.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: newRole }),
      });
      if (!resp.ok) throw new Error('Failed to update role');
      setUsers((prev) => prev.map((u) => u.id === user.id ? { ...u, role: newRole } : u));
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteUser(user) {
    if (!confirm(`Delete user "${user.email}"? This cannot be undone.`)) return;
    try {
      const resp = await authFetch(`/api/admin/users/${user.id}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error('Failed to delete user');
      setUsers((prev) => prev.filter((u) => u.id !== user.id));
    } catch (e) {
      setError(e.message);
    }
  }

  if (!isAdmin) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <Shield className="mx-auto mb-3 h-10 w-10 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">Admin access required.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader title="User Management" subtitle="Manage user accounts and roles" />

      <RoleMetricsCard />

      <div className="mb-4 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{users.length} user(s)</p>
        <Button onClick={() => setShowCreate(true)} size="sm">
          <UserPlus className="mr-1.5 h-4 w-4" /> Add User
        </Button>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/30">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Name</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Email</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Role</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Business Roles</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Created</th>
                <th className="px-4 py-3 text-right font-medium text-muted-foreground">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-border last:border-0 hover:bg-muted/20">
                  <td className="px-4 py-3 font-medium text-foreground">{u.full_name}</td>
                  <td className="px-4 py-3 text-muted-foreground">{u.email}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${u.role === 'admin' ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>
                      {u.role === 'admin' ? <ShieldCheck className="h-3 w-3" /> : <Shield className="h-3 w-3" />}
                      {u.role}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {(u.role_descriptions || []).length === 0 ? (
                        <span className="text-xs text-muted-foreground/50">—</span>
                      ) : (
                        (u.role_descriptions || []).map((r, i) => (
                          <span key={`${r}-${i}`} className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground">
                            <Tag className="h-2.5 w-2.5" />{r}
                          </span>
                        ))
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">{u.created_date ? new Date(u.created_date).toLocaleDateString() : '—'}</td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-1">
                      <button onClick={() => setEditingUser(u)} className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary" title="Edit user">
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button onClick={() => toggleRole(u)} className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
                        {u.role === 'admin' ? 'Demote' : 'Promote'}
                      </button>
                      <button onClick={() => deleteUser(u)} className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-destructive">
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && <CreateUserModal onClose={() => setShowCreate(false)} onCreated={(u) => { setUsers((prev) => [...prev, u]); setShowCreate(false); }} />}

      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onUpdated={(updated) => {
            setUsers((prev) => prev.map((u) => u.id === updated.id ? { ...u, ...updated } : u));
            setEditingUser(null);
          }}
        />
      )}
    </div>
  );
}

function CreateUserModal({ onClose, onCreated }) {
  const [form, setForm] = useState({ email: '', full_name: '', password: '', role: 'user', role_descriptions: [] });
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      const resp = await authFetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'Failed to create user');
      onCreated(data);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-lg" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="font-display text-lg text-foreground">Create New User</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <input required type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          <input required placeholder="Full name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          <input required type="password" placeholder="Password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm">
            <option value="user">User</option>
            <option value="admin">Admin</option>
          </select>
          <div className="block text-xs text-muted-foreground">
            Business Roles
            <RoleTagInput
              value={form.role_descriptions}
              onChange={(roles) => setForm({ ...form, role_descriptions: roles })}
              placeholder="e.g. Financial Analyst"
            />
          </div>
          {err && <p className="text-sm text-destructive">{err}</p>}
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" size="sm" disabled={saving}>{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Create'}</Button>
          </div>
        </form>
      </div>
    </div>
  );
}


function EditUserModal({ user, onClose, onUpdated }) {
  const [form, setForm] = useState({
    email: user.email || '',
    full_name: user.full_name || '',
    role: user.role || 'user',
    password: '',
    role_descriptions: user.role_descriptions || [],
    role_description_text: user.role_description_text || '',
  });
  const [showPassword, setShowPassword] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      // Show warning if admin is demoting themselves
      const { isAdmin } = JSON.parse(localStorage.getItem('zhanlu-auth') || '{}');
      const currentEmail = user.email;
      const selfDemote = form.role === 'user' && user.role === 'admin' && currentEmail === form.email;
      if (selfDemote && !confirm('You are about to remove your own admin role. You will lose admin access. Continue?')) {
        setSaving(false);
        return;
      }

      const body = {
        email: form.email,
        full_name: form.full_name,
        role: form.role,
        role_descriptions: form.role_descriptions || [],
        role_description_text: form.role_description_text || '',
      };
      if (form.password) body.password = form.password;

      const resp = await authFetch(`/api/admin/users/${user.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'Failed to update user');
      onUpdated(data);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-xl border border-border bg-card p-6 shadow-lg" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="font-display text-lg text-foreground">Edit User</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          <label className="block text-xs text-muted-foreground">
            Email
            <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          </label>
          <label className="block text-xs text-muted-foreground">
            Full Name
            <input required value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          </label>
          <label className="block text-xs text-muted-foreground">
            Role
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm">
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <div className="block text-xs text-muted-foreground">
            Business Roles
            <RoleTagInput
              value={form.role_descriptions}
              onChange={(roles) => setForm({ ...form, role_descriptions: roles })}
              placeholder="e.g. Supply Chain Manager"
            />
          </div>

          <label className="block text-xs text-muted-foreground">
            Business Role Description
            <textarea
              value={form.role_description_text}
              onChange={(e) => setForm({ ...form, role_description_text: e.target.value })}
              rows={4}
              placeholder="Optional: describe this user's business role."
              className="mt-1 w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground"
            />
            <span className="mt-1 block text-[11px] text-muted-foreground/70">
              Optional. Used to personalize agent responses.
            </span>
          </label>

          {/* Collapsible password section */}
          <button type="button" onClick={() => setShowPassword(!showPassword)} className="flex w-full items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ChevronDown className={`h-3 w-3 transition-transform ${showPassword ? 'rotate-0' : '-rotate-90'}`} />
            Set new password
          </button>
          {showPassword && (
            <input type="password" placeholder="New password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm" />
          )}

          {err && <p className="text-sm text-destructive">{err}</p>}

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" size="sm" disabled={saving}>{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save'}</Button>
          </div>
        </form>
      </div>
    </div>
  );
}


function RoleTagInput({ value, onChange, placeholder }) {
  const [draft, setDraft] = useState('');
  const roles = Array.isArray(value) ? value : [];

  function add() {
    const text = draft.trim();
    if (!text) return;
    if (roles.some((r) => r.toLowerCase() === text.toLowerCase())) {
      setDraft('');
      return;
    }
    onChange([...roles, text]);
    setDraft('');
  }

  function remove(idx) {
    onChange(roles.filter((_, i) => i !== idx));
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      add();
    } else if (e.key === 'Backspace' && !draft && roles.length) {
      remove(roles.length - 1);
    }
  }

  return (
    <div>
      <div className="flex flex-wrap gap-1.5">
        {roles.map((r, i) => (
          <span key={`${r}-${i}`} className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
            <Tag className="h-2.5 w-2.5" />
            {r}
            <button type="button" onClick={() => remove(i)} className="text-primary/70 hover:text-primary" aria-label={`Remove ${r}`}>
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
      </div>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={add}
        placeholder={placeholder || 'Add role (press Enter)'}
        className="mt-1.5 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
      />
    </div>
  );
}


function RoleMetricsCard() {
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const resp = await authFetch('/api/admin/users/role-feedback-metrics');
        if (resp.ok) setMetrics(await resp.json());
      } catch (_) {
        /* non-critical: hide card if metrics unavailable */
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) {
    return (
      <div className="mb-4 flex items-center gap-2 rounded-xl border border-border px-4 py-3 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading personalization metrics…
      </div>
    );
  }

  if (!metrics || metrics.total_feedback === 0) return null;

  return (
    <div className="mb-4 rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Star className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-medium text-foreground">Role Personalization</h3>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-border bg-muted/20 px-3 py-2">
          <p className="text-[11px] text-muted-foreground">Avg. role relevance</p>
          <p className="text-lg font-semibold text-foreground">
            {metrics.avg_rating != null ? `${metrics.avg_rating} / 5` : '—'}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-muted/20 px-3 py-2">
          <p className="text-[11px] text-muted-foreground">Feedback count</p>
          <p className="text-lg font-semibold text-foreground">{metrics.total_feedback}</p>
        </div>
      </div>
      {metrics.per_role && metrics.per_role.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">Per-role breakdown</p>
          <div className="flex flex-wrap gap-2">
            {metrics.per_role.map((r) => (
              <span key={r.role} className="inline-flex items-center gap-1.5 rounded-full bg-secondary px-2.5 py-1 text-xs text-secondary-foreground">
                {r.role}
                <span className="text-muted-foreground">
                  {r.avg_rating != null ? `${r.avg_rating}/5` : '—'} · {r.count}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
