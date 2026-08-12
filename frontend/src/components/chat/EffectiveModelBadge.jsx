import { useEffect, useState } from "react";
import { Cpu, Lock } from "lucide-react";
import { authFetch } from "@/api/authFetch";

/**
 * Small read-only badge that shows which LLM model is currently active
 * for this conversation (resolved from Project → Agent → user → catalog
 * default → global fallback on the server).
 *
 * Model selection itself lives in the Project / Agent settings — this
 * component only informs the user which model is in use. No editing.
 */
export default function EffectiveModelBadge({ projectId, agentName }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (agentName) params.set("agent_name", agentName);
    authFetch(`/api/llm/effective?${params}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setData(d); })
      .catch(() => { if (!cancelled) setData(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [projectId, agentName]);

  if (loading) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-border bg-secondary/40 px-2 py-1 text-[11px] text-muted-foreground">
        <Cpu className="h-3 w-3" />
        …
      </span>
    );
  }

  // Legacy fallback: project + catalog + global all unset → show global default
  const label = data?.model_name || "Global default";
  const isLocked = data?.locked;
  const isSystemDefault = data?.source === "system_default";

  return (
    <span
      className={`inline-flex max-w-[180px] items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors ${
        isLocked
          ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200"
          : "border-border bg-secondary/40 text-foreground"
      }`}
      title={
        isSystemDefault
          ? "System agent — always uses catalog default"
          : data?.locked_reason || (data?.legacy_fallback ? "Inheriting global default" : "Effective model from project / agent binding")
      }
    >
      {isLocked ? <Lock className="h-3 w-3 shrink-0" /> : <Cpu className="h-3 w-3 shrink-0" />}
      <span className="truncate">{label}</span>
      {isSystemDefault && (
        <span className="ml-0.5 text-[9px] text-muted-foreground">System</span>
      )}
    </span>
  );
}
