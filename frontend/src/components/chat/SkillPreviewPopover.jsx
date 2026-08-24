import { useState, useRef, useEffect } from 'react';
import { ChevronDown, X, BookOpen } from 'lucide-react';

/**
 * SkillPreviewPopover — KIMI-style collapsible methodology preview.
 * Shown next to the active skill chip in the Composer.
 *
 * Props:
 *   skill: the full Tool row (with skill_md, name, description, etc.)
 *   onRemove: callback to detach the skill
 */
export default function SkillPreviewPopover({ skill, onRemove }) {
  const [expanded, setExpanded] = useState(false);
  const wrapRef = useRef(null);

  // Close on outside click
  useEffect(() => {
    if (!expanded) return;
    function handler(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setExpanded(false);
      }
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [expanded]);

  if (!skill) return null;

  const name = skill.name || skill.trigger || 'Skill';
  const desc = skill.description || '';
  const body = skill.skill_md || '';
  // Show first 30 lines as preview
  const previewLines = body.split('\n').slice(0, 30).join('\n');
  const hasMore = body.split('\n').length > 30;

  return (
    <div ref={wrapRef} className="relative inline-flex items-center">
      {/* Chip */}
      <div
        className={`
          flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium
          transition-all duration-200 cursor-pointer select-none
          bg-indigo-50 text-indigo-700 border border-indigo-200/60
          hover:bg-indigo-100 hover:border-indigo-300
          ${expanded ? 'ring-2 ring-indigo-300/40' : ''}
        `}
        onClick={() => setExpanded(!expanded)}
      >
        <BookOpen className="w-3.5 h-3.5" />
        <span className="max-w-[160px] truncate">{name}</span>
        <ChevronDown
          className={`w-3.5 h-3.5 transition-transform duration-200 ${
            expanded ? 'rotate-180' : ''
          }`}
        />
      </div>

      {/* Remove button */}
      <button
        onClick={onRemove}
        className="ml-0.5 p-0.5 rounded-full text-slate-400 hover:text-red-500 hover:bg-red-50 transition-colors"
        title="Remove skill"
      >
        <X className="w-3 h-3" />
      </button>

      {/* Popover */}
      <div
        className={`
          absolute top-full left-0 mt-2 w-[320px] z-50
          bg-white/80 backdrop-blur-md border border-slate-200/60
          rounded-xl shadow-lg shadow-indigo-500/5
          transition-all duration-200 origin-top-left
          ${expanded ? 'opacity-100 scale-100' : 'opacity-0 scale-95 pointer-events-none'}
        `}
      >
        <div className="p-3">
          {/* Header */}
          <div className="flex items-center gap-2 mb-2">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-500 flex items-center justify-center">
              <BookOpen className="w-4 h-4 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <h4 className="text-sm font-semibold text-slate-800 truncate">{name}</h4>
              {skill.trigger && (
                <p className="text-[10px] text-indigo-500 font-medium truncate">
                  /{skill.trigger}
                </p>
              )}
            </div>
            {skill.category && (
              <span className="text-[9px] uppercase tracking-wider text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded">
                {skill.category}
              </span>
            )}
          </div>

          {/* Description */}
          {desc && (
            <p className="text-[11px] text-slate-500 leading-relaxed mb-2">{desc}</p>
          )}

          {/* Methodology preview */}
          {previewLines && (
            <div className="mt-1">
              <div className="flex items-center gap-1.5 mb-1">
                <div className="w-1 h-1 rounded-full bg-indigo-400" />
                <span className="text-[10px] uppercase tracking-wider text-indigo-600 font-semibold">
                  Methodology
                </span>
              </div>
              <div className="bg-slate-50/80 border border-slate-100 rounded-lg p-2.5 max-h-[240px] overflow-y-auto">
                <pre className="text-[10px] text-slate-600 leading-relaxed font-mono whitespace-pre-wrap break-words">
                  {previewLines}
                </pre>
                {hasMore && (
                  <p className="text-[9px] text-slate-400 mt-2 italic">
                    {(body.split('\n').length - 30)} more lines…
                  </p>
                )}
              </div>
            </div>
          )}

          {!previewLines && (
            <p className="text-[10px] text-slate-400 italic">
              Methodology will be loaded when the agent starts.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
