import { useEffect, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { ShieldCheck, Loader2, Info } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useLanguage } from '@/lib/LanguageProvider';
import { authFetch } from '@/api/authFetch';
import { listPolicies, savePolicies, listBoundKbs, listKbTables } from '@/api/accessPolicyApi';
import ResourceAccessPolicyTree from './ResourceAccessPolicyTree';

export default function ResourceAccessPolicyDialog({
  open,
  onClose,
  resourceType,
  resourceId,
  resourceName,
  initialUserId,
}) {
  const { t } = useLanguage();
  const ap = t.accessPolicy || {};

  const [sharedUsers, setSharedUsers] = useState([]);
  const [selectedUserId, setSelectedUserId] = useState('');
  const [selectedKbId, setSelectedKbId] = useState('');
  const [kbs, setKbs] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [loadingKbs, setLoadingKbs] = useState(false);
  const [loadingPolicies, setLoadingPolicies] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [savedMsg, setSavedMsg] = useState('');

  // Reset transient state when the dialog opens
  useEffect(() => {
    if (!open) return;
    setError('');
    setSavedMsg('');
    setPolicies([]);
    setSelectedUserId('');
    setSelectedKbId('');
    setKbs([]);
  }, [open]);

  // Load shared users for this resource
  useEffect(() => {
    if (!open) return;
    setLoadingUsers(true);
    (async () => {
      try {
        const resp = await authFetch(`/api/shares?resource_type=${resourceType}&resource_id=${resourceId}`);
        if (!resp.ok) throw new Error('shares failed');
        const data = await resp.json();
        const map = new Map();
        for (const s of data.given || []) {
          if (!s.shared_with_user_id) continue;
          if (!map.has(s.shared_with_user_id)) {
            map.set(s.shared_with_user_id, {
              id: s.shared_with_user_id,
              name: s.shared_with_name || s.shared_with_email || s.shared_with_user_id,
            });
          }
        }
        const users = [...map.values()];
        setSharedUsers(users);
        if (initialUserId && users.some((u) => u.id === initialUserId)) {
          setSelectedUserId(initialUserId);
        } else if (users.length) {
          setSelectedUserId(users[0].id);
        }
      } catch {
        setError(ap.loadError || 'Failed to load');
      } finally {
        setLoadingUsers(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, resourceType, resourceId, initialUserId]);

  // Load bound KBs + tables once
  useEffect(() => {
    if (!open) return;
    setLoadingKbs(true);
    (async () => {
      try {
        const bound = await listBoundKbs(resourceType, resourceId);
        const withTables = [];
        for (const kb of bound) {
          const tables = await listKbTables(kb.id);
          withTables.push({ id: kb.id, name: kb.name, tables });
        }
        setKbs(withTables);
        if (withTables.length) setSelectedKbId((prev) => prev || withTables[0].id);
      } catch (e) {
        setError(e.message || ap.loadError || 'Failed to load databases');
      } finally {
        setLoadingKbs(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, resourceType, resourceId]);

  // Load policies for the selected user
  useEffect(() => {
    if (!open || !selectedUserId) return;
    setLoadingPolicies(true);
    setError('');
    listPolicies(resourceType, resourceId, selectedUserId)
      .then((p) => setPolicies(p || []))
      .catch((e) => setError(e.message || ap.loadError || 'Failed to load policies'))
      .finally(() => setLoadingPolicies(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedUserId, resourceType, resourceId]);

  async function handleSave() {
    if (!selectedUserId) return;
    setSaving(true);
    setError('');
    setSavedMsg('');
    try {
      await savePolicies(resourceType, resourceId, selectedUserId, policies);
      setSavedMsg(ap.saved || 'Saved');
    } catch (e) {
      setError(e.message || ap.saveError || 'Failed to save');
    } finally {
      setSaving(false);
    }
  }

  const busy = loadingUsers || loadingKbs || loadingPolicies;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-primary" />
            {ap.title || 'Manage Access'}
            {resourceName ? <span className="text-muted-foreground font-normal text-sm">· {resourceName}</span> : null}
          </DialogTitle>
          <DialogDescription className="flex items-start gap-2 text-left">
            <Info className="h-4 w-4 shrink-0 mt-0.5 text-muted-foreground" />
            <span>{ap.defaultAllow}</span>
          </DialogDescription>
        </DialogHeader>

        {loadingUsers ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
            <Loader2 className="h-4 w-4 animate-spin" /> {ap.selectUser || 'Loading users…'}
          </div>
        ) : sharedUsers.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted-foreground">{ap.noShares}</div>
        ) : (
          <div className="flex-1 overflow-y-auto pr-1 space-y-4">
            <div>
              <label className="text-sm font-medium block mb-1">{ap.selectUser || 'Select user'}</label>
              <select
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                value={selectedUserId}
                onChange={(e) => setSelectedUserId(e.target.value)}
              >
                {sharedUsers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name}
                  </option>
                ))}
              </select>
            </div>

            {busy ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
                <Loader2 className="h-4 w-4 animate-spin" /> {ap.databases || 'Loading…'}
              </div>
            ) : (
              <>
                {kbs.length > 1 && (
                  <div
                    role="tablist"
                    aria-label={ap.databases || 'Databases'}
                    className="flex items-center gap-2 overflow-x-auto pb-1 -mx-1 px-1"
                  >
                    {kbs.map((kb) => {
                      const active = kb.id === selectedKbId;
                      return (
                        <button
                          key={kb.id}
                          role="tab"
                          aria-selected={active}
                          type="button"
                          onClick={() => setSelectedKbId(kb.id)}
                          className={cn(
                            'shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer',
                            active
                              ? 'border-primary bg-primary text-primary-foreground'
                              : 'border-border bg-background text-muted-foreground hover:bg-muted'
                          )}
                        >
                          {kb.name}
                        </button>
                      );
                    })}
                  </div>
                )}

                <ResourceAccessPolicyTree
                  key={`${selectedUserId}:${selectedKbId}`}
                  kbs={kbs}
                  activeKbId={selectedKbId}
                  policies={policies}
                  onChange={setPolicies}
                />
              </>
            )}
          </div>
        )}

        {error && <p className="text-sm text-destructive">{error}</p>}
        {savedMsg && <p className="text-sm text-green-600">{savedMsg}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            {ap.cancel || 'Cancel'}
          </Button>
          <Button onClick={handleSave} disabled={saving || busy || !selectedUserId}>
            {saving ? ap.saving || 'Saving…' : ap.save || 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
