import { useState } from 'react';
import { CheckCircle2, Eye, Workflow, Bot, Database, GitBranch, FileBarChart, FileText, Clock, ChevronDown, ChevronUp } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import ArtifactPreviewCard from './ArtifactPreviewCard';

const TYPE_META = {
  automation: { icon: Workflow },
  agent: { icon: Bot },
  kb: { icon: Database },
  flow: { icon: GitBranch },
  report: { icon: FileBarChart },
  file: { icon: FileText },
};

export default function ResultCard({ result, onPreview }) {
  const { t } = useLanguage();
  const [showInlinePreview, setShowInlinePreview] = useState(false);
  const meta = TYPE_META[result.type] || TYPE_META.report;
  const Icon = meta.icon;
  const isDraft = result.draft && !result.id;
  const hasPreviewableId = result.type === 'file' && result.id && !isDraft;

  return (
    <div className="mt-3 rounded-lg border border-primary/30 bg-primary/5 p-3">
      <div className="flex items-center gap-2">
        {isDraft ? <Clock className="h-4 w-4 text-primary" /> : <CheckCircle2 className="h-4 w-4 text-primary" />}
        <span className="text-xs font-medium text-foreground">{isDraft ? t.chat.pendingSave : t.chat.created}</span>
      </div>
      <div className="mt-2 flex items-center justify-between rounded-md border border-border bg-card p-2.5">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm text-foreground">{result.name}</span>
        </div>
        <div className="flex items-center gap-1.5">
          {hasPreviewableId && (
            <button
              onClick={() => setShowInlinePreview(!showInlinePreview)}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground transition-colors hover:bg-secondary"
            >
              {showInlinePreview ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              {t.chat.viewInChat || 'View in chat'}
            </button>
          )}
          <button onClick={() => onPreview?.(result)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground transition-colors hover:bg-secondary">
            <Eye className="h-3 w-3" />
            {t.detail.preview}
          </button>
        </div>
      </div>
      {showInlinePreview && hasPreviewableId && (
        <div className="mt-2">
          <ArtifactPreviewCard artifactId={result.id} />
        </div>
      )}
    </div>
  );
}