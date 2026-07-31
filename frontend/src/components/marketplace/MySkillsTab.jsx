import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Trash2, ExternalLink, BarChart3, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { listMySkills, removeMySkill } from '@/api/marketplace';

export default function MySkillsTab() {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listMySkills();
      setSkills(data.skills || []);
    } catch (e) {
      setError(e.message || 'Failed to load skills');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleRemove = async (skill) => {
    try {
      await removeMySkill(skill.id);
      setSkills(prev => prev.filter(s => s.id !== skill.id));
    } catch (e) {
      setError(e.message);
    }
  };

  if (loading) return <div className="py-8 text-center text-sm text-slate-500">Loading...</div>;
  if (error) return (
    <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
      <AlertCircle className="h-4 w-4 text-red-400" />
      <span className="text-xs text-red-300">{error}</span>
    </div>
  );

  if (skills.length === 0) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-slate-400">No skills yet — browse the sources above and add some!</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {skills.map((sk) => (
        <div key={sk.id} className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">{sk.name}</h3>
          <p className="mt-1 line-clamp-2 text-xs text-slate-400">{sk.description}</p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <button
              onClick={() => navigate(`/skill-agent?skill=${sk.name}`)}
              className="inline-flex items-center gap-1 rounded-lg bg-indigo-500/10 px-2 py-1 text-[10px] text-indigo-400 hover:bg-indigo-500/20"
            >
              <ExternalLink className="h-3 w-3" /> {t.marketplace?.openAgent || 'Open in Skill Agent'}
            </button>
            <button
              onClick={() => navigate(`/skills/executions?skill=${sk.name}`)}
              className="inline-flex items-center gap-1 rounded-lg bg-slate-800 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-700"
            >
              <BarChart3 className="h-3 w-3" /> {t.marketplace?.viewExecs || 'View Executions'}
            </button>
            <button
              onClick={() => handleRemove(sk)}
              className="inline-flex items-center gap-1 rounded-lg bg-red-500/10 px-2 py-1 text-[10px] text-red-400 hover:bg-red-500/20"
            >
              <Trash2 className="h-3 w-3" /> {t.marketplace?.remove || 'Remove'}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
