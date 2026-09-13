import React, { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { appParams } from "@/lib/app-params";
import { useLanguage } from "@/lib/LanguageProvider";
import { Button } from "@/components/ui/button";

/**
 * SSO / OIDC "Continue with <provider>" buttons for the auth pages.
 * Renders nothing when SSO is disabled or no providers are configured
 * (backend returns []), so local email/password login is unchanged.
 */
export default function SsoButtons() {
  const { t } = useLanguage();
  const [providers, setProviders] = useState([]);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/apps/${appParams.appId}/auth/sso/providers`)
      .then((resp) => (resp.ok ? resp.json() : { providers: [] }))
      .then((data) => {
        if (!cancelled) setProviders(data.providers || []);
      })
      .catch(() => {
        if (!cancelled) setProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!providers || providers.length === 0) return null;

  const startSso = (providerId) => {
    const next = new URLSearchParams(window.location.search).get("next");
    const params = new URLSearchParams();
    if (next) params.set("next", next);
    const qs = params.toString();
    const base = `/api/apps/${appParams.appId}/auth/sso/${encodeURIComponent(providerId)}/authorize`;
    window.location.href = qs ? `${base}?${qs}` : base;
  };

  const divider = t.auth.ssoDivider || "or";
  const label = (name) =>
    (t.auth.ssoContinueWith || "Continue with {provider}").replace("{provider}", name);

  return (
    <div className="mb-6">
      <div className="relative mb-4">
        <div className="absolute inset-0 flex items-center">
          <div className="w-full border-t" />
        </div>
        <div className="relative flex justify-center text-xs uppercase">
          <span className="bg-card px-2 text-muted-foreground">{divider}</span>
        </div>
      </div>
      <div className="space-y-2">
        {providers.map((p) => (
          <Button
            key={p.id}
            type="button"
            variant="outline"
            className="w-full h-12 font-medium"
            onClick={() => startSso(p.id)}
          >
            <ShieldCheck className="w-4 h-4 mr-2 text-muted-foreground" aria-hidden="true" />
            {label(p.name)}
          </Button>
        ))}
      </div>
    </div>
  );
}
