import { useState } from 'react';
import { Globe, Loader2, Check, X, AlertCircle, Sparkles } from 'lucide-react';
import { collectSkill } from '@/api/skillStudio';
import { useLanguage } from '@/lib/LanguageProvider';

export default function ScrapeUrlDialog({ open, onClose, onCollected }) {
  const { t } = useLanguage();
  const copy = t.skillAgent.scrape || {};
  const stages = [
    { key: 'navigate', label: copy.stages?.navigate || 'Navigating' },
    { key: 'extract', label: copy.stages?.extract || 'Extracting' },
    { key: 'structure', label: copy.stages?.structure || 'Structuring' },
    { key: 'save', label: copy.stages?.save || 'Saving' },
  ];
  const [url, setUrl] = useState('');
  const [skillName, setSkillName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [currentStage, setCurrentStage] = useState(-1);
  const [result, setResult] = useState(null);

  if (!open) return null;

  async function handleCollect() {
    if (!url.trim()) { setError(copy.urlRequired || 'Please enter a URL'); return; }
    setLoading(true);
    setError('');
    setResult(null);
    setCurrentStage(0);

    // Simulate stage progression for UX feedback
    const stageTimer = setInterval(() => {
      setCurrentStage((prev) => (prev < 3 ? prev + 1 : prev));
    }, 2000);

    try {
      const res = await collectSkill(url.trim(), skillName.trim() || undefined);
      clearInterval(stageTimer);
      setCurrentStage(4); // all done
      setResult(res);
      if (onCollected) onCollected(res);
    } catch (e) {
      clearInterval(stageTimer);
      setError(e.message || copy.collectionFailed || 'Collection failed');
    } finally {
      setLoading(false);
    }
  }

  function handleClose() {
    setUrl('');
    setSkillName('');
    setError('');
    setCurrentStage(-1);
    setResult(null);
    onClose();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-slate-900/60 backdrop-blur-sm"
        onClick={loading ? undefined : handleClose}
      />

      {/* Dialog */}
      <div className="relative z-10 w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
              <Globe className="h-4 w-4 text-primary" />
            </div>
            <h2 className="text-base font-semibold text-foreground">{copy.title || 'Scrape Skill from URL'}</h2>
          </div>
          <button
            onClick={handleClose}
            disabled={loading}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-40"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {result ? (
          /* Success state */
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-lg bg-green-500/10 px-3 py-2">
              <Check className="h-4 w-4 text-green-400" />
              <span className="text-sm text-green-600 dark:text-green-300">{copy.success || 'Skill collected successfully!'}</span>
            </div>
            <div className="space-y-1 rounded-lg bg-secondary/40 p-3">
              <div className="text-xs text-muted-foreground">{copy.skillNameLabel || 'Skill Name'}</div>
              <div className="text-sm font-medium text-foreground">{result.skill_name}</div>
              {result.description && (
                <>
                  <div className="mt-2 text-xs text-muted-foreground">{copy.descriptionLabel || 'Description'}</div>
                  <div className="text-sm text-muted-foreground">{result.description}</div>
                </>
              )}
              {result.scan_findings && (
                <>
                  <div className="mt-2 text-xs text-muted-foreground">{copy.securityScanLabel || 'Security Scan'}</div>
                  <div className={`text-xs ${result.scan_findings.has_critical ? 'text-red-500' : 'text-green-600 dark:text-green-400'}`}>
                    {result.scan_findings.summary}
                  </div>
                </>
              )}
            </div>
            <button
              onClick={handleClose}
              className="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              {copy.done || 'Done'}
            </button>
          </div>
        ) : (
          /* Input + progress state */
          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">{copy.urlLabel || 'Website URL'}</label>
              <input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={copy.urlPlaceholder || 'https://example.com/skill-docs'}
                disabled={loading}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-50"
                autoFocus
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                {copy.skillNameLabel || 'Skill Name'} <span className="text-muted-foreground/70">{copy.optional || '(optional)'}</span>
              </label>
              <input
                type="text"
                value={skillName}
                onChange={(e) => setSkillName(e.target.value)}
                placeholder={copy.skillNamePlaceholder || 'my-collected-skill'}
                disabled={loading}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-50"
              />
            </div>

            {/* Progress stages */}
            {loading && (
              <div className="space-y-2 rounded-lg bg-secondary/40 p-3">
                {stages.map((stage, i) => {
                  const isDone = currentStage > i;
                  const isActive = currentStage === i;
                  return (
                    <div key={stage.key} className="flex items-center gap-2">
                      {isDone ? (
                        <Check className="h-3.5 w-3.5 text-green-500" />
                      ) : isActive ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                      ) : (
                        <div className="h-3.5 w-3.5 rounded-full border border-border" />
                      )}
                      <span className={`text-xs ${isDone ? 'text-muted-foreground' : isActive ? 'text-primary' : 'text-muted-foreground/70'}`}>
                        {stage.label}...
                      </span>
                    </div>
                  );
                })}
              </div>
            )}

            {error && (
              <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
                <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
                <span className="text-xs text-red-500">{error}</span>
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={handleClose}
                disabled={loading}
                className="flex-1 rounded-lg border border-border px-4 py-2 text-sm font-medium text-foreground hover:bg-secondary disabled:opacity-40"
              >
                {copy.cancel || 'Cancel'}
              </button>
              <button
                onClick={handleCollect}
                disabled={loading || !url.trim()}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
              >
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                {loading ? (copy.collecting || 'Collecting...') : (copy.collect || 'Collect')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
