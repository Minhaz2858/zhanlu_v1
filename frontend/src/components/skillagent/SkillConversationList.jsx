import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';

export default function SkillConversationList() {
  const navigate = useNavigate();

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-sidebar">
      <div className="flex items-center gap-2 px-3 py-3">
        <button onClick={() => navigate('/toolkit')} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
          <ArrowLeft className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}