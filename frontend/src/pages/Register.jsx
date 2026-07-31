import React, { useState } from "react";
import { Link } from "react-router-dom";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { UserPlus, Mail, Lock, Loader2, Check, X } from "lucide-react";
import { InputOTP, InputOTPGroup, InputOTPSlot } from "@/components/ui/input-otp";
import AuthLayout from "@/components/AuthLayout";
import { toast } from "@/components/ui/use-toast";

// Mirror of backend app/services/password_policy.py (plan 2026-07-27).
function validatePassword(pw) {
  const errs = [];
  if (pw.length < 10) errs.push("at least 10 characters");
  if (!/[A-Za-z]/.test(pw)) errs.push("a letter");
  if (!/\d/.test(pw)) errs.push("a digit");
  return errs;
}

const RULES = [
  { label: "At least 10 characters", test: (pw) => pw.length >= 10 },
  { label: "Contains a letter", test: (pw) => /[A-Za-z]/.test(pw) },
  { label: "Contains a digit", test: (pw) => /\d/.test(pw) },
];

export default function Register() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showOtp, setShowOtp] = useState(false);
  const [otpCode, setOtpCode] = useState("");

  const pwErrors = validatePassword(password);
  const passwordsMatch = password === confirmPassword;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (pwErrors.length > 0) {
      setError("Password must " + pwErrors.join(", ") + ".");
      return;
    }
    if (!passwordsMatch) {
      setError("Passwords do not match");
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
          setError(e2.message || "Failed to send verification code");
        }
      } else {
        setError(msg || "Registration failed");
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
      setError(err.message || "Invalid verification code");
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
    setError("");
    try {
      await base44.auth.resendOtp(email);
      toast({
        title: "Code sent",
        description: "Check your email for the new code.",
      });
    } catch (err) {
      setError(err.message || "Failed to resend code");
    }
  };

  if (showOtp) {
    return (
      <AuthLayout
        icon={Mail}
        title="Verify your email"
        subtitle={`We sent a code to ${email}`}
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
              Verifying...
            </>
          ) : (
            "Verify"
          )}
        </Button>
        <p className="text-center text-sm text-muted-foreground mt-4">
          Didn't receive the code?{" "}
          <button onClick={handleResend} className="text-primary font-medium hover:underline">
            Resend
          </button>
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      icon={UserPlus}
      title="Create your account"
      subtitle="Sign up to get started"
      footer={
        <>
          Already have an account?{" "}
          <Link to="/login" className="text-primary font-medium hover:underline">
            Log in
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
          <Label htmlFor="email">Email</Label>
          <div className="relative">
            <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" aria-hidden="true" />
            <Input
              id="email"
              type="email"
              autoComplete="email"
              autoFocus
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="pl-10 h-12"
              required
            />
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">Password</Label>
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
          <Label htmlFor="confirm">Confirm Password</Label>
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
            <p className="text-xs text-destructive">Passwords do not match</p>
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
              Creating account...
            </>
          ) : (
            "Create account"
          )}
        </Button>
      </form>
    </AuthLayout>
  );
}
