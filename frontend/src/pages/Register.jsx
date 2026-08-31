import React, { useState } from "react";
import { Link } from "react-router-dom";
import { base44 } from "@/api/base44Client";
import { useLanguage } from "@/lib/LanguageProvider";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { UserPlus, Mail, Lock, Loader2, Check, X } from "lucide-react";
import { InputOTP, InputOTPGroup, InputOTPSlot } from "@/components/ui/input-otp";
import AuthLayout from "@/components/AuthLayout";
import { toast } from "@/components/ui/use-toast";

export default function Register() {
  const { t } = useLanguage();
  const { allowPublicRegistration } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showOtp, setShowOtp] = useState(false);
  const [otpCode, setOtpCode] = useState("");

  // Mirror of backend app/services/password_policy.py (plan 2026-07-27).
  const validatePassword = (pw) => {
    const errs = [];
    if (pw.length < 10) errs.push(t.auth.pwMinLen);
    if (!/[A-Za-z]/.test(pw)) errs.push(t.auth.pwHasLetter);
    if (!/\d/.test(pw)) errs.push(t.auth.pwHasDigit);
    return errs;
  };
  const pwErrors = validatePassword(password);
  const passwordsMatch = password === confirmPassword;

  const RULES = [
    { label: t.auth.pwMinLen, test: (pw) => pw.length >= 10 },
    { label: t.auth.pwHasLetter, test: (pw) => /[A-Za-z]/.test(pw) },
    { label: t.auth.pwHasDigit, test: (pw) => /\d/.test(pw) },
  ];

  // When self-registration is disabled (enterprise provisioning), block the
  // form entirely and explain that an admin must create the account.
  if (!allowPublicRegistration) {
    return (
      <AuthLayout
        icon={UserPlus}
        title={t.auth.registrationDisabled}
        subtitle={t.auth.registrationDisabledSubtitle}
        footer={
          <Link to="/login" className="text-primary font-medium hover:underline">
            {t.auth.logIn}
          </Link>
        }
      >
        <p className="text-sm text-muted-foreground text-center">
          {t.auth.registrationDisabledNote}
        </p>
      </AuthLayout>
    );
  }

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (pwErrors.length > 0) {
      setError(t.auth.passwordMust + pwErrors.join(", ") + ".");
      return;
    }
    if (!passwordsMatch) {
      setError(t.auth.pwNoMatch);
      return;
    }
    setLoading(true);
    // The backend register endpoint requires full_name; derive it from the
    // email so we don't add a separate field to the form. For the FIRST user
    // (no accounts yet) the backend returns tokens immediately and skips OTP;
    // in that case we set the token and go home right away.
    const full_name = email.split("@")[0] || email;
    try {
      const result = await base44.auth.register({ email, password, full_name });
      if (result?.access_token) {
        base44.auth.setToken(result.access_token);
        if (result.refresh_token) {
          localStorage.setItem("refresh_token", result.refresh_token);
        }
        window.location.href = "/";
        return;
      }
      setShowOtp(true);
    } catch (err) {
      const msg = err.message || "";
      // Users already exist → backend demands an OTP code first. Send one
      // and reveal the OTP field.
      if (/otp/i.test(msg)) {
        try {
          await base44.auth.resendOtp(email);
          setShowOtp(true);
        } catch (e2) {
          setError(e2.message || t.auth.failedToSendCode);
        }
      } else {
        setError(msg || t.auth.registrationFailed);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async () => {
    setError("");
    setLoading(true);
    // Verify by re-calling register WITH the otp_code — the backend verifies
    // the code, creates the user, and returns the tokens in one call.
    const full_name = email.split("@")[0] || email;
    try {
      const result = await base44.auth.register({ email, password, full_name, otp_code: otpCode });
      if (result?.access_token) {
        base44.auth.setToken(result.access_token);
        if (result.refresh_token) {
          localStorage.setItem("refresh_token", result.refresh_token);
        }
      }
      window.location.href = "/";
    } catch (err) {
      setError(err.message || t.auth.invalidVerificationCode);
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
    setError("");
    try {
      await base44.auth.resendOtp(email);
      toast({
        title: t.auth.codeSent,
        description: t.auth.codeSentDesc,
      });
    } catch (err) {
      setError(err.message || t.auth.failedToResendCode);
    }
  };

  if (showOtp) {
    return (
      <AuthLayout
        icon={Mail}
        title={t.auth.verifyEmail}
        subtitle={t.auth.weSentCode.replace("{email}", email)}
      >
        {error && (
          <div className="mb-4 p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
            {error}
          </div>
        )}
        <div className="flex justify-center mb-6">
          <InputOTP
            maxLength={6}
            value={otpCode}
            onChange={setOtpCode}
            autoFocus
            autoComplete="one-time-code"
          >
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
          className="w-full h-12 font-medium"
          onClick={handleVerify}
          disabled={loading || otpCode.length < 6}
        >
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              {t.auth.verifying}
            </>
          ) : (
            t.auth.verify
          )}
        </Button>
        <p className="text-center text-sm text-muted-foreground mt-4">
          {t.auth.didntReceive}{" "}
          <button onClick={handleResend} className="text-primary font-medium hover:underline">
            {t.auth.resend}
          </button>
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      icon={UserPlus}
      title={t.auth.createAccount}
      subtitle={t.auth.signUpToStart}
      footer={
        <>
          {t.auth.alreadyHaveAccount}{" "}
          <Link to="/login" className="text-primary font-medium hover:underline">
            {t.auth.logIn}
          </Link>
        </>
      }
    >
      {error && (
        <div className="mb-4 p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
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
          <Label htmlFor="password">{t.auth.password}</Label>
          <div className="relative">
            <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" aria-hidden="true" />
            <Input
              id="password"
              type="password"
              autoComplete="new-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="pl-10 h-12"
              required
            />
          </div>
          {password.length > 0 && (
            <ul className="mt-2 space-y-1">
              {RULES.map((rule) => {
                const ok = rule.test(password);
                return (
                  <li
                    key={rule.label}
                    className={`flex items-center text-xs ${ok ? "text-emerald-600" : "text-muted-foreground"}`}
                  >
                    {ok ? (
                      <Check className="w-3 h-3 mr-1.5" />
                    ) : (
                      <X className="w-3 h-3 mr-1.5" />
                    )}
                    {rule.label}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirm">{t.auth.confirmPassword}</Label>
          <div className="relative">
            <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" aria-hidden="true" />
            <Input
              id="confirm"
              type="password"
              autoComplete="new-password"
              placeholder="••••••••"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="pl-10 h-12"
              required
            />
          </div>
          {confirmPassword.length > 0 && !passwordsMatch && (
            <p className="text-xs text-destructive">{t.auth.pwNoMatch}</p>
          )}
        </div>
        <Button
          type="submit"
          className="w-full h-12 font-medium"
          disabled={loading || pwErrors.length > 0 || !passwordsMatch}
        >
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              {t.auth.creatingAccount}
            </>
          ) : (
            t.auth.createAccount
          )}
        </Button>
      </form>
    </AuthLayout>
  );
}
