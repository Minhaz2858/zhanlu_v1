import { X, Check, Plus } from 'lucide-react';

export default function SkillDetailSheet({ skill, open, onClose, onInstall, isInstalled }) {
  if (!open || !skill) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-xl border border-slate-800 bg-slate-900 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">{skill.display_name || skill.name}</h2>
            <p className="mt-1 text-sm text-slate-400">{skill.description}</p>
            <div className="mt-2 flex gap-3 text-xs text-slate-500">
              <span>Category: {skill.category}</span>
              <span>Version: {skill.version}</span>
              {skill.author && <span>Author: {skill.author}</span>}
            </div>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-slate-500 hover:bg-slate-800">
            <X className="h-4 w-4" />
          </button>
        </div>

        <pre className="mt-4 max-h-[50vh] overflow-auto rounded-lg bg-slate-950 p-4 text-xs text-slate-300 whitespace-pre-wrap">
          {skill.skill_md || 'No preview available'}
        </pre>

        <div className="mt-4 flex justify-end">
          <button
            onClick={() => onInstall?.(skill)}
            disabled={isInstalled}
            className={`inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium transition ${
              isInstalled
                ? 'cursor-default bg-emerald-500/10 text-emerald-400'
                : 'bg-indigo-500 text-white hover:bg-indigo-600'
            }`}
          >
            {isInstalled ? <Check className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
            {isInstalled ? 'Added to My Skills' : 'Add to My Skills'}
          </button>
        </div>
      </div>
    </div>
  );
}
