import { useLanguage } from '@/lib/LanguageProvider';
import PageHeader from '@/components/PageHeader';
import ProjectsView from '@/components/project/ProjectsView';
import AgentsView from '@/components/agent/AgentsView';
import { Building2 } from 'lucide-react';

/**
 * FromCompanyPage — company-wide resources assigned to the current user.
 * Filters to resource_type === 'company' or is_shared_with_me === true.
 */
export default function FromCompanyPage() {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={t.sidebar.fromCompany}
        subtitle={isEn
          ? 'Company-wide resources assigned to you — projects and agents configured by your admin.'
          : '由公司分配的资源——管理员配置的项目与智能体。'}
      />
      <div className="mt-2 mb-6 inline-flex items-center gap-1.5 rounded-full bg-secondary px-2.5 py-1 text-xs text-muted-foreground">
        <Building2 className="h-3 w-3" />
        <span>{isEn ? 'Admin configured' : '管理员配置'}</span>
      </div>
      <ProjectsView scope="company" />
      <div className="mt-12">
        <AgentsView scope="company" />
      </div>
    </div>
  );
}