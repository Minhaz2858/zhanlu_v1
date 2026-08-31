import React, { useState } from "react";
import { Link } from "react-router-dom";
import { appParams } from "@/lib/app-params";
import { useLanguage } from "@/lib/LanguageProvider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Mail, ArrowLeft, Loader2 } from "lucide-react";
import AuthLayout from "@/components/AuthLayout";

export default function ForgotPassword() {
  const { t } = useLanguage();
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      await fetch(`/api/apps/${appParams.appId}/auth/reset-password-request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      // The endpoint always returns a generic {sent:true} (even for unknown
      // emails) to prevent account enumeration, so we always show the note.
    } catch {
      // Network error — still show the generic note rather than leak state.
    } finally {
      setLoading(false);
      setSent(true);
    }
  };

  return (
    <AuthLayout
      icon={Mail}
      title={t.auth.resetPassword}
      subtitle={t.auth.resetSubtitle}
      footer={
        <Link to="/login" className="text-primary font-medium hover:underline">
          <ArrowLeft className="w-3 h-3 inline mr-1" />{t.auth.backToLogin}
        </Link>
      }
    >
      {sent ? (
        <p className="text-sm text-foreground text-center">
          {t.auth.resetSentNote}
        </p>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">{t.auth.emailAddress}</Label>
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" aria-hidden="true" />
              <Input
                id="email"
                type="email"
                autoComplete="email"
                autoFocus
                placeholder={t.auth.emailPh}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="pl-10 h-12"
                required
              />
            </div>
          </div>
          <Button type="submit" className="w-full h-12 font-medium" disabled={loading}>
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t.auth.sending}
              </>
            ) : (
              t.auth.sendResetLink
            )}
          </Button>
        </form>
      )}
    </AuthLayout>
  );
}
