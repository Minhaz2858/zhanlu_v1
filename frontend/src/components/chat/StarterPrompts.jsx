import { useState, useEffect } from 'react';
import { Sparkles } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

/**
 * Starter prompts — AI-literacy chips on the welcome screen (IBM Gap 4).
 *
 * "Questions that worked for your team" (mined from this deployment's
 * successful turns) plus capability starters. Clicking a chip fills the
 * composer — the user reviews and sends. Fail-quiet: on any error the
 * component renders nothing (literacy must never block chatting).
 */

export default function StarterPrompts({ onPick }) {
  const [starters, setStarters] = useState([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch('/api/literacy/starter-prompts');
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled && Array.isArray(data.starters)) {
          setStarters(data.starters.slice(0, 4));
        }
      } catch {
        /* fail-quiet — no chips is always safe */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Disabled (2026-10-31): the "Try asking — the first ones are questions
  // that already worked here" chip strip was removed from the UI by user
  // request. The component still returns null so Chat.jsx needs no edit,
  // and the implementation is kept intact below in case it is re-enabled.
  return null;

  if (starters.length === 0) return null;

  return (
    <div className="mb-3 w-full max-w-2xl">
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-stone-400">
        <Sparkles size={11} className="text-amber-500" />
        Try asking — the first ones are questions that already worked here
      </div>
      <div className="flex flex-wrap gap-1.5">
        {starters.map((s, i) => (
          <button
            key={i}
            onClick={() => onPick(s.question)}
            title={s.source === 'your_team' ? 'A question that worked for your team' : 'Suggested'}
            className={`max-w-full truncate rounded-full border px-3 py-1.5 text-xs transition-colors ${
              s.source === 'your_team'
                ? 'border-amber-200 bg-amber-50/70 text-amber-800 hover:bg-amber-100'
                : 'border-stone-200 bg-white text-stone-600 hover:bg-stone-100'
            }`}
          >
            {s.question}
          </button>
        ))}
      </div>
    </div>
  );
}
