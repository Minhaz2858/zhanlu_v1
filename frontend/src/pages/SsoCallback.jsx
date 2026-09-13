import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, Loader2, LogIn } from "lucide-react";
import { base44 } from "@/api/base44Client";
import AuthLayout from "@/components/AuthLayout";
import { useLanguage } from "@/lib/LanguageProvider";

const REASON_MESSAGES = {
  email_not_allowed: "email_not_allowed",
  email_not_verified: "email_not_verified",
  invalid_id_token: "invalid_id_token",
  token_exchange_failed: "token_exchange_failed",
};

/**
 * SSO callback landing page. The backend 302s here with tokens in the URL
 * FRAGMENT (never query strings) — this page stores them exactly like the
 * normal login flow (base44.auth.setToken + localStorage refresh_token)
 * then redirects to ?next= or the role-based landing page.
 */
export default function SsoCallback() {
  const { t } = useLanguage();
  const [error, setError] = useState("");

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const backendError = query.get("error");
    if (backendError) {
      setError(REASON_MESSAGES[backendError] || backendError);
      return;
    }

    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const accessToken = fragment.get("access_token");
    const refreshToken = fragment.get("refresh_token");
    if (!accessToken) {
      setError("missing_token");
      return;
    }
    base44.auth.setToken(accessToken);
    if (refreshToken) localStorage.setItem("refresh_token", refreshToken);

    let user = null;
    try {
      user = fragment.get("user") ? JSON.parse(fragment.get("user")) : null;
    } catch {
      user = null;
    }
    const next = fragment.get("next");
    if (next) {
      window.location.href = decodeURIComponent(next);
      return;
    }
    const role = user?.role;
    window.location.href = role === "admin" ? "/admin/users" : "/";
  }, []);

  if (!error) {
    return (
      <AuthLayout
        icon={LogIn}
        title={t.auth.ssoSigningInTitle || "Signing you in…"}
        subtitle={t.auth.ssoSigningInSub || ""}
      >
        <div className="flex justify-center py-6">
          <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" aria-hidden="true" />
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout icon={AlertCircle} title={t.auth.ssoFailedTitle || "Sign-in failed"}>
      <div className="space-y-4 text-center">
        <p className="text-sm text-muted-foreground">
          {(t.auth.ssoFailedSub || "Single sign-on failed ({reason}).")
            .replace("{reason}", error)}
        </p>
        <Link
          to="/login"
          className="inline-flex items-center justify-center w-full h-12 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90"
        >
          {t.auth.ssoBackToLogin || "Back to login"}
        </Link>
      </div>
    </AuthLayout>
  );
}
