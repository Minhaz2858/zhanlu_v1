import { useState } from 'react';
import { BarChart3, Users } from 'lucide-react';
import CustomerSalesReport from './CustomerSalesReport';
import ContractPerformanceReport from './ContractPerformanceReport';

const REPORT_ORGS = [
  { value: 'huizhou', label: '惠州伊斯科' },
  { value: 'guangdong', label: '广东伊斯科' },
];

const TABS = [
  { key: 'customer_sales', label: '客户销售情况', icon: Users },
  { key: 'contract_performance', label: '销售合同执行情况', icon: BarChart3 },
];

export default function ReportView() {
  const [activeTab, setActiveTab] = useState('customer_sales');
  const [org, setOrg] = useState('huizhou');

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* Tab bar + org selector */}
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-white dark:bg-slate-900">
        <div className="flex items-center justify-between px-6 py-2">
          <div className="flex items-center gap-1">
            {TABS.map(({ key, label, icon: Icon }) => {
              const active = activeTab === key;
              return (
                <button
                  key={key}
                  onClick={() => setActiveTab(key)}
                  className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                    active
                      ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </button>
              );
            })}
          </div>
          {/* Org selector */}
          <select
            value={org}
            onChange={(e) => setOrg(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
          >
            {REPORT_ORGS.map(({ value, label }) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 px-6 py-4">
        {activeTab === 'customer_sales' && (
          <CustomerSalesReport org={org} />
        )}
        {activeTab === 'contract_performance' && (
          <ContractPerformanceReport org={org} />
        )}
      </div>
    </div>
  );
}
