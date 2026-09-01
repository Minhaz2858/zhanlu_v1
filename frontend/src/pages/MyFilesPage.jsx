import { useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import PageHeader from '@/components/PageHeader';
import FilesView from '@/components/files/FilesView';
import { User, Building2 } from 'lucide-react';

/**
 * MyFilesPage — standalone page for all agent-generated files and artifacts.
 *
 * Two internal tabs:
 *   - "From Personal" — files from personal agents (default scope)
 *   - "From Company"  — files from company agents
 *
 * Files without a project are treated as personal (rule chosen during
 * brainstorming). Filtering is delegated to <FilesView scope="..."/>.
 */
export default function MyFilesPage() {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  const [scope, setScope] = useState('personal');

  const TABS = [
    { key: 'personal', label: t.sidebar.fromPersonal || (isEn ? 'From Personal' : '来自个人'), icon: User },
    { key: 'company',  label: t.sidebar.fromCompany  || (isEn ? 'From Company'  : '来自公司'),  icon: Building2 },
  ];

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={t.sidebar.myFiles || (isEn ? 'My Files' : '我的文件')}
        subtitle={isEn
          ? 'Every resource and asset your agents have built or generated — reports, dashboards, automation outputs and more.'
          : '你的智能体创建或生成的全部资源与资产——报表、仪表盘、自动化产物等。'}
      />

      {/* Tabs: From Personal / From Company */}
      <div className="mb-6 inline-flex rounded-lg border border-border bg-card p-1">
        {TABS.map((tab) => {
          const active = scope === tab.key;
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              onClick={() => setScope(tab.key)}
              className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                active ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
              }`}
              aria-pressed={active}
            >
              <Icon className="h-3.5 w-3.5" />
              {tab.label}
            </button>
          );
        })}
      </div>

      <FilesView scope={scope} />
    </div>
  );
}
