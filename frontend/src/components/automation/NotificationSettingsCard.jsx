import { useState } from 'react';
import { Mail, Pencil, Plus, X } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { toast } from '@/components/ui/use-toast';
import { authFetch } from '@/api/authFetch';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Input } from '@/components/ui/input';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';

const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;

const NOTIFY_ON_OPTIONS = [
  { value: 'always', en: 'Always', zh: '每次运行' },
  { value: 'on_success', en: 'On success only', zh: '仅在成功后' },
  { value: 'on_failure', en: 'On failure only', zh: '仅在失败后' },
];

function notifyOnLabel(value, isEn) {
  const opt = NOTIFY_ON_OPTIONS.find((o) => o.value === value);
  return opt ? (isEn ? opt.en : opt.zh) : value;
}

/**
 * NotificationSettingsCard — per-task email notification settings.
 *
 * View mode summarises the current configuration; Edit mode is an inline form
 * (matching the Basic Info card flow): gateway enable switch, notify_on select,
 * validated recipient chips, and an attach-file switch. Saving PATCHes
 * ``/api/automations/{id}/notification-settings``.
 */
export default function NotificationSettingsCard({ task, onSaved }) {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(!!task?.notify_enabled);
  const [notifyOn, setNotifyOn] = useState(task?.notify_on || 'always');
  const [attachFile, setAttachFile] = useState(task?.attach_file !== false);
  const [emails, setEmails] = useState(() =>
    Array.isArray(task?.notify_emails) ? [...task.notify_emails] : [],
  );
  const [draft, setDraft] = useState('');
  const [invalid, setInvalid] = useState([]);

  function addEmails(raw) {
    const parts = String(raw || '')
      .split(/[,;\s]+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    if (!parts.length) return;
    const bad = parts.filter((p) => !EMAIL_RE.test(p));
    const good = parts.filter((p) => EMAIL_RE.test(p));
    setInvalid(bad);
    if (good.length) setEmails((prev) => Array.from(new Set([...prev, ...good])));
    setDraft('');
  }

  function removeEmail(addr) {
    setEmails((prev) => prev.filter((e) => e !== addr));
  }

  function cancel() {
    setEditing(false);
    setEnabled(!!task?.notify_enabled);
    setNotifyOn(task?.notify_on || 'always');
    setAttachFile(task?.attach_file !== false);
    setEmails(Array.isArray(task?.notify_emails) ? [...task.notify_emails] : []);
    setDraft('');
    setInvalid([]);
  }

  async function save() {
    const clean = emails.map((e) => e.trim().toLowerCase()).filter(Boolean);
    const bad = clean.filter((e) => !EMAIL_RE.test(e));
    if (bad.length) {
      setInvalid(bad);
      toast({
        title: isEn ? 'Invalid email address' : '邮箱地址无效',
        description: isEn
          ? `"${bad.join('", "')}" ${bad.length === 1 ? 'is' : 'are'} not valid.`
          : `"${bad.join('", "')}" 不是有效的邮箱地址。`,
        variant: 'destructive',
      });
      return;
    }
    setSaving(true);
    try {
      const res = await authFetch(`/api/automations/${task.id}/notification-settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          notify_enabled: enabled,
          notify_emails: clean,
          notify_on: notifyOn,
          attach_file: attachFile,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      toast({
        title: isEn ? 'Notifications updated' : '通知设置已保存',
        description: isEn ? 'Email settings saved.' : '邮件设置已保存。',
      });
      setEditing(false);
      setInvalid([]);
      onSaved?.();
    } catch (e) {
      console.error('[NotificationSettingsCard] save failed:', e);
      toast({ title: isEn ? 'Save failed' : '保存失败', description: e.message, variant: 'destructive' });
    } finally {
      setSaving(false);
    }
  }

  const gatewayBadge = enabled
    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400'
    : 'bg-secondary text-muted-foreground';

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <header className="mb-4 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-primary" aria-hidden="true" />
          <h3 className="text-sm font-semibold text-foreground">
            {isEn ? 'Notifications' : '通知设置'}
          </h3>
        </div>
        {!editing ? (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="inline-flex items-center gap-1 rounded-md p-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            aria-label={isEn ? 'Edit notifications' : '编辑通知设置'}
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
            {isEn ? 'Edit' : '编辑'}
          </button>
        ) : (
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={cancel} disabled={saving}>
              {isEn ? 'Cancel' : '取消'}
            </Button>
            <Button size="sm" onClick={save} disabled={saving || invalid.length > 0}>
              {saving ? (isEn ? 'Saving…' : '保存中…') : isEn ? 'Save' : '保存'}
            </Button>
          </div>
        )}
      </header>

      {!editing ? (
        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">{isEn ? 'Email gateway' : '邮件网关'}</span>
            <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${gatewayBadge}`}>
              {enabled ? (isEn ? 'Enabled' : '已启用') : isEn ? 'Disabled' : '已禁用'}
            </span>
          </div>
          <div className="flex items-center justify-between border-t border-border pt-3">
            <span className="text-muted-foreground">{isEn ? 'Send email' : '发送邮件'}</span>
            <span className="text-foreground">{notifyOnLabel(notifyOn, isEn)}</span>
          </div>
          <div className="border-t border-border pt-3">
            <div className="mb-1.5 text-muted-foreground">{isEn ? 'Recipients' : '收件人'}</div>
            {emails.length ? (
              <div className="flex flex-wrap gap-1.5">
                {emails.map((e) => (
                  <span
                    key={e}
                    className="inline-flex items-center rounded-full bg-secondary px-2 py-0.5 text-xs text-foreground"
                  >
                    {e}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                {isEn ? 'No recipients configured' : '未配置收件人'}
              </p>
            )}
          </div>
          <div className="flex items-center justify-between border-t border-border pt-3">
            <span className="text-muted-foreground">{isEn ? 'Attach output file' : '附带输出文件'}</span>
            <span className="text-foreground">
              {attachFile ? (isEn ? 'Attach file' : '附带文件') : isEn ? 'Download link' : '下载链接'}
            </span>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-foreground">
                {isEn ? 'Email gateway' : '邮件网关'}
              </div>
              <div className="text-xs text-muted-foreground">
                {isEn ? 'Send run result emails to the recipients below' : '向以下收件人发送运行结果邮件'}
              </div>
            </div>
            <Switch
              checked={enabled}
              onCheckedChange={setEnabled}
              aria-label={isEn ? 'Email gateway' : '邮件网关'}
            />
          </div>

          <div className={enabled ? '' : 'pointer-events-none opacity-50'}>
            <div className="mb-1.5 text-xs text-muted-foreground">{isEn ? 'Send email' : '发送邮件'}</div>
            <Select value={notifyOn} onValueChange={setNotifyOn}>
              <SelectTrigger className="w-full" aria-label={isEn ? 'Send email' : '发送邮件'}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {NOTIFY_ON_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {isEn ? o.en : o.zh}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <div className="mt-4">
              <div className="mb-1.5 text-xs text-muted-foreground">
                {isEn ? 'Recipients' : '收件人'}
              </div>
              {emails.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {emails.map((e) => (
                    <span
                      key={e}
                      className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-foreground"
                    >
                      {e}
                      <button
                        type="button"
                        onClick={() => removeEmail(e)}
                        className="text-muted-foreground transition-colors hover:text-red-600"
                        aria-label={isEn ? `Remove ${e}` : `移除 ${e}`}
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <div className="flex gap-2">
                <Input
                  value={draft}
                  onChange={(ev) => setDraft(ev.target.value)}
                  onKeyDown={(ev) => {
                    if (ev.key === 'Enter' || ev.key === ',') {
                      ev.preventDefault();
                      addEmails(draft);
                    }
                  }}
                  placeholder={isEn ? 'user@example.com, boss@example.com' : 'user@example.com, boss@example.com'}
                  className="h-9 flex-1 text-sm"
                  aria-label={isEn ? 'Add email addresses' : '添加邮箱地址'}
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-9"
                  onClick={() => addEmails(draft)}
                  disabled={!draft.trim()}
                >
                  <Plus className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
                  {isEn ? 'Add' : '添加'}
                </Button>
              </div>
              {invalid.length > 0 && (
                <p className="mt-1.5 text-xs text-red-600" role="alert">
                  {isEn
                    ? `Invalid: ${invalid.join(', ')}`
                    : `无效的邮箱地址：${invalid.join(', ')}`}
                </p>
              )}
              <p className="mt-1 text-[11px] text-muted-foreground">
                {isEn
                  ? 'Separate multiple addresses with commas.'
                  : '多个邮箱地址请用逗号分隔。'}
              </p>
            </div>

            <div className="mt-4 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-foreground">
                  {isEn ? 'Attach output file' : '附带输出文件'}
                </div>
                <div className="text-xs text-muted-foreground">
                  {isEn ? 'Files over 8 MB are sent as a download link.' : '超过 8 MB 的文件以下载链接形式发送。'}
                </div>
              </div>
              <Switch
                checked={attachFile}
                onCheckedChange={setAttachFile}
                aria-label={isEn ? 'Attach output file' : '附带输出文件'}
              />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
