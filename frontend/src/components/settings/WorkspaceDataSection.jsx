import { useState, useEffect } from 'react';
import { Loader2, Check } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { authFetch } from '@/api/authFetch';

/**
 * WorkspaceDataSection — workspace-level data-source access settings.
 *
 * Currently exposes the ``auto_bind_all_datasources`` opt-in flag
 * (DATA-CORE-3). When ON, every connected database KnowledgeBase in
 * the workspace is automatically added to every agent's bound list at
 * runtime, so the user doesn't have to remember to bind each KB to
 * each agent manually. Default is OFF — the explicit-binding UX is
 * preserved unless the user opts in.
 *
 * The flag is a workspace-level setting, NOT a per-user setting. The
 * UI here is in My Space → Settings because that's where global
 * "My Space" rules live (per the existing sections pattern). The
 * actual storage is a single row in the ``workspace_settings`` table
 * keyed by (org_id, app_id, key).
 */
export default function WorkspaceDataSection() {
  const { lang } = useLanguage();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [autoBind, setAutoBind] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const resp = await authFetch('/api/workspace-settings');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (!cancelled) setAutoBind(!!data.auto_bind_all_datasources);
      } catch (e) {
        if (!cancelled) setError(e?.message || 'Failed to load settings');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  async function save(next) {
    setSaving(true);
    setError(null);
    try {
      const resp = await authFetch('/api/workspace-settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_bind_all_datasources: next }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setAutoBind(next);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(e?.message || 'Failed to save setting');
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {lang === 'en' ? 'Loading workspace settings…' : '正在加载工作区设置…'}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-border bg-card p-6">
        <div className="flex items-start justify-between gap-6">
          <div className="flex-1">
            <h3 className="font-display text-base text-foreground">
              {lang === 'en'
                ? 'Allow every agent to read from every connected database'
                : '允许所有智能体读取所有已连接数据库'}
            </h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {lang === 'en'
                ? 'When on, every database KnowledgeBase connected in My Space → Databases & KB is automatically added to every agent\'s data source list at runtime. Agents with no explicit bindings will still be able to query every connected DB. Default is OFF — agents only see databases the user has explicitly bound.'
                : '开启后，「我的空间 → 数据库与知识库」中连接的所有数据库知识库都会在运行时自动加入每个智能体的数据源列表。尚未显式绑定任何数据库的智能体，也能够查询所有已连接的数据库。默认关闭——智能体仅可见用户显式绑定的数据库。'}
            </p>
            <p className="mt-2 text-[11px] text-muted-foreground">
              {lang === 'en'
                ? 'DATA-CORE-3: each agent can only use datasources the user has authorized. This flag is the workspace-wide authorization.'
                : 'DATA-CORE-3：每个智能体仅可使用用户授权的数据源。本开关是工作区级授权。'}
            </p>
            {error && (
              <p className="mt-2 text-xs text-destructive">{error}</p>
            )}
          </div>
          <div className="flex flex-col items-end gap-2">
            <button
              role="switch"
              aria-checked={autoBind}
              onClick={() => save(!autoBind)}
              disabled={saving}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors disabled:opacity-50 ${
                autoBind ? 'bg-primary' : 'bg-muted'
              }`}
              title={
                lang === 'en'
                  ? autoBind
                    ? 'Currently ON — every agent can read every connected DB'
                    : 'Currently OFF — agents can only read explicitly bound DBs'
                  : autoBind
                    ? '当前已开启：所有智能体可读取所有已连接数据库'
                    : '当前已关闭：智能体仅可读取显式绑定的数据库'
              }
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition-transform ${
                  autoBind ? 'translate-x-5' : 'translate-x-0.5'
                } mt-0.5`}
              />
            </button>
            <div className="flex h-4 items-center text-[10px] text-muted-foreground">
              {saving ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  {lang === 'en' ? 'Saving…' : '保存中…'}
                </>
              ) : saved ? (
                <>
                  <Check className="mr-1 h-3 w-3 text-primary" />
                  {lang === 'en' ? 'Saved' : '已保存'}
                </>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
