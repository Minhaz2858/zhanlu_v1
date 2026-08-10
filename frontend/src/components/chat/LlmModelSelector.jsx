import { useState, useEffect } from "react";
import { useLanguage } from "@/lib/LanguageProvider";
import { authFetch } from "@/api/authFetch";

/**
 * Simplest possible model picker — a native <select>.
 *
 * Selecting a model immediately calls onChange(id) so it is "saved on click"
 * with no extra confirm step. The parent decides what to persist
 * (e.g. base44.entities.Project.update).
 *
 * Props:
 *   value (string|null) — current llm_model_id
 *   onChange (id|null) — called when selection changes
 *   disabled (bool) — disable the control
 *   showLabel (bool) — render a "Default Model" label above
 *   className (string) — extra classes
 */
export default function LlmModelSelector({
  value,
  onChange,
  disabled = false,
  showLabel = true,
  className = "",
}) {
  const { t } = useLanguage();
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    authFetch("/api/llm/models")
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => { if (!cancelled) setModels(Array.isArray(data) ? data : []); })
      .catch(() => { if (!cancelled) setModels([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const isEn = (t.common?.yes === "Yes") || (t.common?.no === "No");
  const label = (t.settings?.llmBinding?.title) || "Default Model";
  const placeholder = (t.settings?.llmBinding?.placeholder) || "Default (use system default LLM)";
  const emptyText = isEn ? "No models — add one in Settings" : "尚未配置模型 — 请在设置中添加";

  return (
    <div className={className}>
      {showLabel && (
        <label className="mb-1 block text-xs font-medium text-muted-foreground">
          {label}
        </label>
      )}
      <select
        disabled={disabled || loading}
        value={value || ""}
        onChange={(e) => onChange?.(e.target.value || null)}
        className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary disabled:cursor-not-allowed disabled:opacity-60"
      >
        {loading ? (
          <option value="">{isEn ? "Loading…" : "加载中…"}</option>
        ) : models.length === 0 ? (
          <option value="">{emptyText}</option>
        ) : (
          <>
            <option value="">{placeholder}</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </>
        )}
      </select>
    </div>
  );
}
