import { useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  Bell,
  Settings,
  SlidersHorizontal,
  Webhook,
  X,
} from 'lucide-react';
import { getAlertPreferences, updateAlertPreferences } from '@/api/alertApi';

const SEVERITIES = [
  { value: 'low', label: 'Low', hint: 'Surface everything' },
  { value: 'medium', label: 'Medium', hint: 'Meaningful moves only' },
  { value: 'high', label: 'High', hint: 'Critical moves only' },
];

const CHANNELS = [
  { value: 'in_app', label: 'In-app panel', icon: Bell },
  { value: 'webhook', label: 'Webhook', icon: Webhook },
];

/**
 * Modal for tuning per-user alert preferences: severity floor, channels,
 * sensitivity multiplier, quiet hours and recommendations.
 */
export default function AlertPreferencesModal({ open, onClose, onSaved }) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [minSeverity, setMinSeverity] = useState('low');
  const [channels, setChannels] = useState(['in_app']);
  const [multiplier, setMultiplier] = useState(1.0);
  const [quietHours, setQuietHours] = useState({
    enabled: false,
    start: '22:00',
    end: '07:00',
  });
  const [recommendationsEnabled, setRecommendationsEnabled] = useState(true);
  const [webhookUrl, setWebhookUrl] = useState('');

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    getAlertPreferences()
      .then((prefs) => {
        if (cancelled) return;
        setMinSeverity(prefs.min_severity || 'low');
        setChannels(prefs.channels?.length ? prefs.channels : ['in_app']);
        setMultiplier(prefs.threshold_multiplier ?? 1.0);
        setQuietHours(
          prefs.quiet_hours || { enabled: false, start: '22:00', end: '07:00' },
        );
        setRecommendationsEnabled(prefs.recommendations_enabled ?? true);
        setWebhookUrl(prefs.webhook_url || '');
      })
      .catch((err) => {
        console.error('Failed to load alert preferences:', err);
        toast.error('Could not load alert preferences');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  if (!open) return null;

  const toggleChannel = (value) => {
    setChannels((prev) =>
      prev.includes(value) ? prev.filter((c) => c !== value) : [...prev, value],
    );
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload = {
        min_severity: minSeverity,
        channels,
        threshold_multiplier: Number(multiplier),
        quiet_hours: quietHours,
        recommendations_enabled: recommendationsEnabled,
        webhook_url: webhookUrl.trim() || null,
      };
      const updated = await updateAlertPreferences(payload);
      toast.success('Alert preferences saved');
      onSaved?.(updated);
      onClose?.();
    } catch (err) {
      console.error('Failed to save alert preferences:', err);
      toast.error('Could not save alert preferences');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Alert preferences"
    >
      <div className="w-full max-w-lg max-h-[90vh] overflow-y-auto bg-white rounded-2xl shadow-xl border border-slate-200">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <Settings className="h-4 w-4 text-indigo-600" />
            <h2 className="text-sm font-semibold text-slate-800">
              Alert Preferences
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-slate-400 hover:text-slate-600 rounded transition-colors"
            aria-label="Close preferences"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="px-5 py-4 space-y-5">
          {loading ? (
            <div className="py-10 text-center text-xs text-slate-400">
              Loading preferences…
            </div>
          ) : (
            <>
              {/* Severity floor */}
              <section>
                <h3 className="text-xs font-semibold text-slate-700 mb-2">
                  Minimum severity
                </h3>
                <div className="grid grid-cols-3 gap-2">
                  {SEVERITIES.map((s) => (
                    <label
                      key={s.value}
                      className={`cursor-pointer rounded-lg border px-3 py-2 text-center transition-colors ${
                        minSeverity === s.value
                          ? 'border-indigo-500 bg-indigo-50 text-indigo-700'
                          : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
                      }`}
                    >
                      <input
                        type="radio"
                        name="min_severity"
                        value={s.value}
                        checked={minSeverity === s.value}
                        onChange={() => setMinSeverity(s.value)}
                        className="sr-only"
                      />
                      <span className="block text-xs font-semibold">
                        {s.label}
                      </span>
                      <span className="block text-[10px] text-slate-400 mt-0.5">
                        {s.hint}
                      </span>
                    </label>
                  ))}
                </div>
              </section>

              {/* Channels */}
              <section>
                <h3 className="text-xs font-semibold text-slate-700 mb-2">
                  Notification channels
                </h3>
                <div className="space-y-2">
                  {CHANNELS.map((c) => {
                    const Icon = c.icon;
                    const checked = channels.includes(c.value);
                    return (
                      <label
                        key={c.value}
                        className={`flex items-center gap-2.5 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
                          checked
                            ? 'border-indigo-500 bg-indigo-50'
                            : 'border-slate-200 hover:border-slate-300'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleChannel(c.value)}
                          className="accent-indigo-600"
                        />
                        <Icon className="h-4 w-4 text-slate-500" />
                        <span className="text-xs font-medium text-slate-700">
                          {c.label}
                        </span>
                      </label>
                    );
                  })}
                </div>
                {channels.includes('webhook') && (
                  <div className="mt-2">
                    <label
                      htmlFor="webhook-url"
                      className="block text-[11px] text-slate-500 mb-1"
                    >
                      Webhook URL
                    </label>
                    <input
                      id="webhook-url"
                      type="url"
                      value={webhookUrl}
                      onChange={(e) => setWebhookUrl(e.target.value)}
                      placeholder="https://hooks.example.com/…"
                      className="w-full text-xs border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <p className="text-[10px] text-slate-400 mt-1">
                      Signed with HMAC-SHA256; a secret is generated automatically.
                    </p>
                  </div>
                )}
              </section>

              {/* Sensitivity */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-xs font-semibold text-slate-700">
                    Sensitivity
                  </h3>
                  <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-indigo-600">
                    <SlidersHorizontal className="h-3 w-3" />
                    {Number(multiplier).toFixed(1)}x
                  </span>
                </div>
                <input
                  type="range"
                  aria-label="Sensitivity"
                  min="0.5"
                  max="2.0"
                  step="0.1"
                  value={multiplier}
                  onChange={(e) => setMultiplier(e.target.value)}
                  className="w-full accent-indigo-600"
                />
                <div className="flex justify-between text-[10px] text-slate-400 mt-1">
                  <span>0.5x quieter</span>
                  <span>1.0x default</span>
                  <span>2.0x louder</span>
                </div>
              </section>

              {/* Quiet hours */}
              <section>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={quietHours.enabled}
                    onChange={(e) =>
                      setQuietHours((prev) => ({
                        ...prev,
                        enabled: e.target.checked,
                      }))
                    }
                    className="accent-indigo-600"
                  />
                  <span className="text-xs font-semibold text-slate-700">
                    Quiet hours (no webhooks)
                  </span>
                </label>
                {quietHours.enabled && (
                  <div className="flex items-center gap-2 mt-2">
                    <label className="text-[11px] text-slate-500" htmlFor="qh-start">
                      From
                    </label>
                    <input
                      id="qh-start"
                      type="time"
                      value={quietHours.start}
                      onChange={(e) =>
                        setQuietHours((prev) => ({
                          ...prev,
                          start: e.target.value,
                        }))
                      }
                      className="text-xs border border-slate-200 rounded-lg px-2 py-1.5"
                    />
                    <label className="text-[11px] text-slate-500" htmlFor="qh-end">
                      To
                    </label>
                    <input
                      id="qh-end"
                      type="time"
                      value={quietHours.end}
                      onChange={(e) =>
                        setQuietHours((prev) => ({
                          ...prev,
                          end: e.target.value,
                        }))
                      }
                      className="text-xs border border-slate-200 rounded-lg px-2 py-1.5"
                    />
                  </div>
                )}
              </section>

              {/* Recommendations */}
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={recommendationsEnabled}
                  onChange={(e) => setRecommendationsEnabled(e.target.checked)}
                  className="accent-indigo-600"
                />
                <span className="text-xs font-semibold text-slate-700">
                  Show recommendations
                </span>
              </label>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-100">
          <button
            onClick={onClose}
            className="text-xs font-semibold text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg px-4 py-2 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || loading}
            className="text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 rounded-lg px-4 py-2 transition-colors"
            aria-label="Save preferences"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
