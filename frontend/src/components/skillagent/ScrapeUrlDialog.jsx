import { useState } from 'react';
import { Globe, Loader2, Check, X, AlertCircle, Sparkles } from 'lucide-react';
import { collectSkill } from '@/api/skillStudio';
import { useLanguage } from '@/lib/LanguageProvider';

const STAGES = [
  { key: 'navigate', label: 'Navigating' },
  { key: 'extract', label: 'Extracting' },
  { key: 'structure', label: 'Structuring' },
  { key: 'save', label: 'Saving' },
];

export default function ScrapeUrlDialog({ open, onClose, onCollected }) {
  const { t } = useLanguage();
  const [url, setUrl] = useState('');
  const [skillName, setSkillName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [currentStage, setCurrentStage] = useState(-1);
  const [result, setResult] = useState(null);

  if (!open) return null;

  async function handleCollect() {
    if (!url.trim()) { setError('Please enter a URL'); return; }
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
      setError(e.message || 'Collection failed');
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
      <div className="relative z-10 w-full max-w-md rounded-2xl border border-slate-700 bg-slate-800/95 p-6 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500/20">
              <Globe className="h-4 w-4 text-indigo-400" />
            </div>
            <h2 className="text-base font-semibold text-slate-100">Scrape Skill from URL</h2>
          </div>
          <button
            onClick={handleClose}
            disabled={loading}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 hover:bg-slate-700 hover:text-slate-200 disabled:opacity-40"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {result ? (
          /* Success state */
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-lg bg-green-500/10 px-3 py-2">
              <Check className="h-4 w-4 text-green-400" />
              <span className="text-sm text-green-300">Skill collected successfully!</span>
            </div>
            <div className="space-y-1 rounded-lg bg-slate-900/50 p-3">
              <div className="text-xs text-slate-400">Skill Name</div>
              <div className="text-sm font-medium text-slate-100">{result.skill_name}</div>
              {result.description && (
                <>
                  <div className="mt-2 text-xs text-slate-400">Description</div>
                  <div className="text-sm text-slate-300">{result.description}</div>
                </>
              )}
              {result.scan_findings && (
                <>
                  <div className="mt-2 text-xs text-slate-400">Security Scan</div>
                  <div className={`text-xs ${result.scan_findings.has_critical ? 'text-red-400' : 'text-green-400'}`}>
                    {result.scan_findings.summary}
                  </div>
                </>
              )}
            </div>
            <button
              onClick={handleClose}
              className="w-full rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-600"
            >
              Done
            </button>
          </div>
        ) : (
          /* Input + progress state */
          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">Website URL</label>
              <input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/skill-docs"
                disabled={loading}
                className="w-full rounded-lg border border-slate-600 bg-slate-900/50 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
                autoFocus
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Skill Name <span className="text-slate-500">(optional)</span>
              </label>
              <input
                type="text"
                value={skillName}
                onChange={(e) => setSkillName(e.target.value)}
                placeholder="my-collected-skill"
                disabled={loading}
                className="w-full rounded-lg border border-slate-600 bg-slate-900/50 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
              />
            </div>

            {/* Progress stages */}
            {loading && (
              <div className="space-y-2 rounded-lg bg-slate-900/50 p-3">
                {STAGES.map((stage, i) => {
                  const isDone = currentStage > i;
                  const isActive = currentStage === i;
                  return (
                    <div key={stage.key} className="flex items-center gap-2">
                      {isDone ? (
                        <Check className="h-3.5 w-3.5 text-green-400" />
                      ) : isActive ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-indigo-400" />
                      ) : (
                        <div className="h-3.5 w-3.5 rounded-full border border-slate-600" />
                      )}
                      <span className={`text-xs ${isDone ? 'text-slate-400' : isActive ? 'text-indigo-300' : 'text-slate-500'}`}>
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
                <span className="text-xs text-red-300">{error}</span>
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={handleClose}
                disabled={loading}
                className="flex-1 rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                onClick={handleCollect}
                disabled={loading || !url.trim()}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-600 disabled:opacity-40"
              >
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                {loading ? 'Collecting...' : 'Collect'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
