import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { authFetch } from '@/api/authFetch';
import PageHeader from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Loader2, UserPlus, Trash2, Shield, ShieldCheck, X, Pencil, Eye, ChevronDown } from 'lucide-react';

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
  const [form, setForm] = useState({ email: '', full_name: '', password: '', role: 'user' });
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
  });
  const [showPassword, setShowPassword] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const [detail, setDetail] = useState(null); // { owned, granted }
  const [detailLoading, setDetailLoading] = useState(true);
  const [tab, setTab] = useState('owned'); // 'owned' | 'granted'

  // Load owned + granted resources
  useEffect(() => {
    async function loadDetail() {
      setDetailLoading(true);
      try {
        const resp = await authFetch(`/api/admin/users/${user.id}`);
        if (resp.ok) {
          const data = await resp.json();
          setDetail({ owned: data.owned || [], granted: data.granted || [] });
        }
      } catch (_) { /* non-critical */ }
      finally { setDetailLoading(false); }
    }
    loadDetail();
  }, [user.id]);

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

      const body = { email: form.email, full_name: form.full_name, role: form.role };
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

  const ownedCount = detail?.owned?.length || 0;
  const grantedCount = detail?.granted?.length || 0;

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

        {/* Resources section */}
        <div className="mt-5 border-t border-border pt-4">
          <h3 className="mb-3 flex items-center gap-1.5 text-sm font-medium text-foreground">
            <Eye className="h-3.5 w-3.5" />
            Resources
          </h3>

          {/* Tabs */}
          <div className="mb-3 flex rounded-lg border border-border bg-muted/30 p-0.5">
            <button onClick={() => setTab('owned')} className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${tab === 'owned' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'}`}>
              Owned ({ownedCount})
            </button>
            <button onClick={() => setTab('granted')} className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${tab === 'granted' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'}`}>
              Granted ({grantedCount})
            </button>
          </div>

          {detailLoading ? (
            <div className="flex justify-center py-4"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          ) : tab === 'owned' ? (
            <ResourceList items={detail?.owned || []} badge="Owner" emptyText="No resources owned by this user." />
          ) : (
            <ResourceList items={detail?.granted || []} badge="Shared" emptyText="No resources shared with this user." showGrantedBy />
          )}
        </div>
      </div>
    </div>
  );
}


function ResourceList({ items, badge, emptyText, showGrantedBy }) {
  if (items.length === 0) {
    return <p className="py-3 text-center text-xs text-muted-foreground">{emptyText}</p>;
  }
  return (
    <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
      {items.map((r, i) => (
        <div key={`${r.type}-${r.id}-${i}`} className="flex items-center justify-between border-b border-border px-3 py-2 last:border-0 text-sm">
          <div className="min-w-0 flex-1">
            <p className="truncate text-foreground">{r.name}</p>
            {showGrantedBy && r.granted_by_email && (
              <p className="truncate text-[11px] text-muted-foreground">by {r.granted_by_email}</p>
            )}
          </div>
          <span className="ml-2 shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {badge}
          </span>
        </div>
      ))}
    </div>
  );
}
