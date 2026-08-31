import React, { useState } from "react";
import { Link } from "react-router-dom";
import { base44 } from "@/api/base44Client";
import { appParams } from "@/lib/app-params";
import { useLanguage } from "@/lib/LanguageProvider";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LogIn, Mail, Lock, Loader2, KeyRound } from "lucide-react";
import AuthLayout from "@/components/AuthLayout";
import { InputOTP, InputOTPGroup, InputOTPSlot } from "@/components/ui/input-otp";
import { toast } from "@/components/ui/use-toast";

export default function Login() {
  const { t } = useLanguage();
  const { allowPublicRegistration } = useAuth();
  const [mode, setMode] = useState("code"); // "code" (SaaS primary) | "password"
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [codeStep, setCodeStep] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);

  const finishLogin = (data) => {
    base44.auth.setToken(data.access_token);
    if (data.refresh_token) {
      localStorage.setItem("refresh_token", data.refresh_token);
    }
    const next = new URLSearchParams(window.location.search).get("next");
    if (next) {
      window.location.href = decodeURIComponent(next);
      return;
    }
    // Role-based landing: admins land in the admin console, users in the workspace.
    const role = data.user?.role;
    window.location.href = role === "admin" ? "/admin/users" : "/";
  };

  const handlePasswordSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const resp = await fetch(`/api/apps/${appParams.appId}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || t.auth.invalidCredentials);
      finishLogin(data);
    } catch (err) {
      setError(err.message || t.auth.invalidCredentials);
    } finally {
      setLoading(false);
    }
  };

  const requestCode = async () => {
    setError("");
    setSending(true);
    try {
      const resp = await fetch(`/api/apps/${appParams.appId}/auth/request-login-code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await resp.json().catch(() => ({}));
      // 400 = missing email; everything else (incl. unknown email) proceeds to step 2.
      if (resp.status === 400) {
        setError(data.detail || t.auth.email || "Email required");
        return false;
      }
      return true;
    } catch (err) {
      console.error("request-login-code failed:", err);
      return true; // allow the user to reach the code step and retry
    } finally {
      setSending(false);
    }
  };

  const handleSendCode = async (e) => {
    e.preventDefault();
    if (!email) {
      setError(t.auth.email || "Email required");
      return;
    }
    const ok = await requestCode();
    if (ok) setCodeStep(true);
  };

  const handleCodeLogin = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const resp = await fetch(`/api/apps/${appParams.appId}/auth/login-with-code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, code }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || t.auth.invalidLoginCode);
      finishLogin(data);
    } catch (err) {
      setError(err.message || t.auth.invalidLoginCode);
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
    setError("");
    await requestCode();
    toast({ title: t.auth.codeSent, description: t.auth.codeSentDesc });
  };

  const ModeToggle = (
    <div className="flex items-center gap-1 p-1 mb-6 bg-muted rounded-xl">
      <button
        type="button"
        onClick={() => setMode("code")}
        className={`flex-1 flex items-center justify-center gap-2 h-10 rounded-lg text-sm font-medium transition-colors ${
          mode === "code"
            ? "bg-primary text-primary-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground"
        }`}
      >
        <Mail className="w-4 h-4" /> {t.auth.emailCodeTab}
      </button>
      <button
        type="button"
        onClick={() => setMode("password")}
        className={`flex-1 flex items-center justify-center gap-2 h-10 rounded-lg text-sm font-medium transition-colors ${
          mode === "password"
            ? "bg-primary text-primary-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground"
        }`}
      >
        <KeyRound className="w-4 h-4" /> {t.auth.passwordTab}
      </button>
    </div>
  );

  const errorBanner = error && (
    <div className="mb-4 p-3 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
  );

  return (
    <AuthLayout
      icon={LogIn}
      title={t.auth.welcomeBack}
      subtitle={t.auth.logInToAccount}
      footer={
        mode === "password" && allowPublicRegistration ? (
          <>
            {t.auth.noAccount}{" "}
            <Link to="/register" className="text-primary font-medium hover:underline">
              {t.auth.createOne}
            </Link>
          </>
        ) : (
          <></>
        )
      }
    >
      {errorBanner}
      {ModeToggle}

      {mode === "password" ? (
        <form onSubmit={handlePasswordSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">{t.auth.email}</Label>
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
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="password">{t.auth.password}</Label>
              <Link to="/forgot-password" className="text-xs text-primary hover:underline">
                {t.auth.forgotPassword}
              </Link>
            </div>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" aria-hidden="true" />
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="pl-10 h-12"
                required
              />
            </div>
          </div>
          <Button type="submit" className="w-full h-12 font-medium" disabled={loading}>
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t.auth.loggingIn}
              </>
            ) : (
              t.auth.logIn
            )}
          </Button>
        </form>
      ) : !codeStep ? (
        <form onSubmit={handleSendCode} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">{t.auth.email}</Label>
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
          <Button type="submit" className="w-full h-12 font-medium" disabled={sending}>
            {sending ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t.auth.sendingCode}
              </>
            ) : (
              t.auth.sendCode
            )}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            {t.auth.usePassword}{" "}
            <button
              type="button"
              onClick={() => setMode("password")}
              className="text-primary font-medium hover:underline"
            >
              {t.auth.passwordTab}
            </button>
          </p>
        </form>
      ) : (
        <form onSubmit={handleCodeLogin} className="space-y-5">
          <p className="text-center text-sm text-muted-foreground">
            {t.auth.enterCodeDesc.replace("{email}", email)}
          </p>
          <div className="flex justify-center">
            <InputOTP maxLength={6} value={code} onChange={setCode} autoFocus autoComplete="one-time-code">
              <InputOTPGroup>
                <InputOTPSlot index={0} />
                <InputOTPSlot index={1} />
                <InputOTPSlot index={2} />
                <InputOTPSlot index={3} />
                <InputOTPSlot index={4} />
                <InputOTPSlot index={5} />
              </InputOTPGroup>
            </InputOTP>
          </div>
          <Button
            type="submit"
            className="w-full h-12 font-medium"
            disabled={loading || code.length < 6}
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t.auth.loggingIn}
              </>
            ) : (
              t.auth.logIn
            )}
          </Button>
          <div className="flex items-center justify-between text-sm">
            <button
              type="button"
              onClick={() => setCodeStep(false)}
              className="text-muted-foreground hover:text-foreground"
            >
              {t.auth.email}
            </button>
            <button
              type="button"
              onClick={handleResend}
              className="text-primary font-medium hover:underline"
            >
              {t.auth.resend}
            </button>
          </div>
        </form>
      )}
    </AuthLayout>
  );
}
