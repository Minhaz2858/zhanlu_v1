import { useState, useRef, useEffect, useCallback } from "react";
import { ChevronDown, Cpu, Zap, Brain, Sparkles, Lock, Server, Loader2 } from "lucide-react";
import { useLanguage } from "@/lib/LanguageProvider";
import { authFetch } from "@/api/authFetch";

const AVAILABLE_MODELS = [
  {
    id: "gpt-4o-mini",
    label: "GPT-4o Mini",
    provider: "openai",
    descriptionKey: "models.gpt4oMiniDesc",
    icon: Zap,
  },
  {
    id: "gpt-4o",
    label: "GPT-4o",
    provider: "openai",
    descriptionKey: "models.gpt4oDesc",
    icon: Brain,
  },
  {
    id: "deepseek-chat",
    label: "DeepSeek Chat",
    provider: "deepseek",
    descriptionKey: "models.dsChatDesc",
    icon: Cpu,
  },
  {
    id: "deepseek-reasoner",
    label: "DeepSeek Reasoner",
    provider: "deepseek",
    descriptionKey: "models.dsReasonerDesc",
    icon: Sparkles,
  },
  {
    id: "claude-3-5-sonnet-20241022",
    label: "Claude 3.5 Sonnet",
    provider: "anthropic",
    descriptionKey: "models.claudeSonnetDesc",
    icon: Brain,
  },
];

const STORAGE_KEY = "zhanlu-preferred-model";

/**
 * Dropdown to select the LLM model for the current conversation.
 *
 * When ``locked`` is true (hierarchical LLM config enforces an admin-set model),
 * the switcher renders as a read-only badge showing the effective model.
 *
 * Saves preference to localStorage so it persists across sessions.
 * The selected model is sent as a ``model`` field in the stream request body.
 *
 * Usage in Chat.jsx header:
 *   <ModelSwitcher onModelChange={setPreferredModel}
 *                  projectId={projectId}
 *                  agentName={agentName} />
 */
export default function ModelSwitcher({
  onModelChange,
  className = "",
  projectId,
  agentName,
}) {
  const { t } = useLanguage();
  const [model, setModel] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || "gpt-4o-mini";
    } catch {
      return "gpt-4o-mini";
    }
  });
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Hierarchical LLM state
  const [effectiveLLM, setEffectiveLLM] = useState(null); // null = not yet loaded
  const [effLoading, setEffLoading] = useState(false);

  // Fetch effective LLM only when projectId/agentName change
  useEffect(() => {
    let cancelled = false;
    async function fetchEff() {
      setEffLoading(true);
      try {
        const params = new URLSearchParams();
        if (projectId) params.set("project_id", projectId);
        if (agentName) params.set("agent_name", agentName);
        params.set("user_model", model);
        const res = await authFetch(`/api/llm/effective?${params}`);
        if (!cancelled && res.ok) {
          const data = await res.json();
          setEffectiveLLM(data);
        } else if (!cancelled) {
          // Flag off or API unavailable — show normal switcher
          setEffectiveLLM(null);
        }
      } catch {
        if (!cancelled) setEffectiveLLM(null);
      } finally {
        if (!cancelled) setEffLoading(false);
      }
    }
    fetchEff();
    return () => { cancelled = true; };
  }, [projectId, agentName, model]);

  const selected = AVAILABLE_MODELS.find((m) => m.id === model) || AVAILABLE_MODELS[0];

  const handleSelect = useCallback(
    (id) => {
      setModel(id);
      try {
        localStorage.setItem(STORAGE_KEY, id);
      } catch {
        /* ignore */
      }
      setOpen(false);
      onModelChange?.(id);
    },
    [onModelChange],
  );

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const Icon = selected.icon;

  // Resolve description string from the chat.models block
  const getModelDesc = (m) => {
    const models = t.chat?.models || {};
    const key = m.descriptionKey?.split(".").pop();
    return key ? models[key] : m.provider;
  };

  const badge = t.settings?.llmBadge || {};
  const isLocked = effectiveLLM?.locked && !effectiveLLM?.legacy_fallback;

  // Loading state
  if (effLoading && effectiveLLM === undefined) {
    return (
      <div ref={ref} className={`relative ${className}`}>
        <div className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-400 dark:border-gray-700 dark:bg-gray-800">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>{badge.fetching || "Loading…"}</span>
        </div>
      </div>
    );
  }

  // Locked badge (admin-managed model, non-admin user)
  if (isLocked) {
    const displayName = effectiveLLM?.model_name || selected.label;
    const isPrivate = effectiveLLM?.is_private;
    return (
      <div ref={ref} className={`relative ${className}`}>
        <div
          className="flex items-center gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs dark:border-amber-800 dark:bg-amber-900/30"
          title={effectiveLLM?.locked_reason || badge.locked}
        >
          {isPrivate ? <Server className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" /> : <Lock className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" />}
          <span className="max-w-[100px] truncate font-medium text-amber-800 dark:text-amber-200">{displayName}</span>
          {isPrivate && (
            <span className="rounded-full bg-amber-200 px-1.5 py-0.5 text-[9px] font-medium text-amber-700 dark:bg-amber-800 dark:text-amber-300">
              {badge.private || "Private"}
            </span>
          )}
          <span className="rounded bg-amber-200 px-1 py-0.5 text-[9px] font-semibold text-amber-700 dark:bg-amber-800 dark:text-amber-300">
            {badge.locked || "Locked"}
          </span>
        </div>
      </div>
    );
  }

  // Normal switcher with effective LLM info when available
  const effModel = effectiveLLM?.legacy_fallback === false ? effectiveLLM : null;

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 hover:border-gray-300 hover:bg-gray-50 transition-colors dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-750"
        title={effModel ? `${t.chat?.models?.title || "Model"}: ${effModel.model_name}` : (t.chat?.models?.title || "Select model")}
      >
        <Icon className="h-3.5 w-3.5 text-gray-400" />
        <span className="max-w-[100px] truncate">
          {effModel ? `${effModel.model_name} ← ${selected.label}` : selected.label}
        </span>
        <ChevronDown className={`h-3 w-3 text-gray-400 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-56 rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800">
          <div className="p-1">
            <div className="px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
              {t.chat?.models?.title || "Model"}
            </div>
            {effModel && (
              <div className="mx-2 mb-1 mt-0.5 rounded bg-blue-50 px-2 py-1 text-[10px] text-blue-600 dark:bg-blue-900/30 dark:text-blue-300">
                {badge.locked ? `${badge.locked}: ${effModel.model_name}` : effModel.model_name}
              </div>
            )}
            {AVAILABLE_MODELS.map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => handleSelect(m.id)}
                className={`flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${
                  m.id === model
                    ? "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
                    : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-750"
                }`}
              >
                <m.icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-gray-400" />
                <div className="min-w-0">
                  <div className="font-medium truncate">{m.label}</div>
                  <div className="text-[10px] text-gray-400 mt-0.5">
                    {getModelDesc(m) || m.provider}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
