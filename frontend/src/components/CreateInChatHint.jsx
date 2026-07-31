import { Link } from 'react-router-dom';
import { MessageSquare, ArrowRight } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function CreateInChatHint({ text }) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center justify-between gap-4 rounded-xl border border-border bg-secondary/60 px-5 py-4">
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        <MessageSquare className="h-4 w-4 text-primary" />
        <span>{text || t.createHint.default}</span>
      </div>
      <Link to="/" className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm font-medium text-primary hover:underline">
        {t.common.goChat} <ArrowRight className="h-4 w-4" />
      </Link>
    </div>
  );
}