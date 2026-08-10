import { Check, Plus } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * SkillCard — a single skill on the Browse Marketplace grid.
 *
 * Visual contract: neutral dark card matching the rest of the marketplace
 * (same tokens as SourceCard). No brand-color tint on the action button —
 * the Add / Added states are differentiated only by background and weight,
 * not by hue. The title hover uses the theme primary (warm orange) for
 * affordance.
 */
export default function SkillCard({ skill, isInstalled, onInstall, onOpenDetail }) {
  const { t } = useLanguage();
  const tags = (skill.tags || []).slice(0, 3);

  return (
    <div className="group relative flex flex-col rounded-xl border border-border bg-card p-4 transition hover:border-foreground/30">
      <div className="flex items-start justify-between gap-2">
        <h3
          className="min-w-0 flex-1 cursor-pointer break-words text-sm font-semibold text-foreground hover:text-primary"
          onClick={() => onOpenDetail?.(skill)}
        >
          {skill.display_name || skill.name}
        </h3>
        <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
          {skill.category}
        </span>
      </div>

      <p className="mt-1 break-words line-clamp-2 text-xs text-muted-foreground">{skill.description}</p>

      {tags.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {tags.map((tag) => (
            <span key={tag} className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
              #{tag}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex items-center justify-between">
        <span className="text-[10px] text-muted-foreground">
          {skill.author ? `${skill.author} · ` : ''}v{skill.version}
        </span>
        <button
          onClick={() => onInstall?.(skill)}
          disabled={isInstalled}
          className={`inline-flex items-center gap-1 rounded-lg px-3 py-1 text-xs font-medium transition ${
            isInstalled
              ? 'cursor-default text-muted-foreground'
              : 'bg-secondary text-foreground hover:bg-accent'
          }`}
        >
          {isInstalled ? <Check className="h-3 w-3" /> : <Plus className="h-3 w-3" />}
          {isInstalled ? (t.marketplace?.added || 'Added') : (t.marketplace?.add || 'Add')}
        </button>
      </div>
    </div>
  );
}
