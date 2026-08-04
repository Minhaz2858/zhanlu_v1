import { useLanguage } from '@/lib/LanguageProvider';
import PageHeader from '@/components/PageHeader';
import ProjectsView from '@/components/project/ProjectsView';
import AgentsView from '@/components/agent/AgentsView';
import { User } from 'lucide-react';

/**
 * FromPersonalPage — resources owned by the current user only.
 * Projects and agents filtered to resource_type !== 'company' and not shared with me.
 */
export default function FromPersonalPage() {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={t.sidebar.fromPersonal}
        subtitle={isEn
          ? 'Your private resources — projects and agents you have created.'
          : '你的私人资源——你创建的项目与智能体。'}
      />
      <div className="mt-2 mb-6 inline-flex items-center gap-1.5 rounded-full bg-secondary px-2.5 py-1 text-xs text-muted-foreground">
        <User className="h-3 w-3" />
        <span>{isEn ? 'Owned by you' : '由你所有'}</span>
      </div>
      <ProjectsView scope="personal" />
      <div className="mt-12">
        <AgentsView scope="personal" />
      </div>
    </div>
  );
}