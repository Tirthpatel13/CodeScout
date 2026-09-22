"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { apiFetch, ApiError } from "@/lib/api";

function CallbackInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    const code = params.get("code");
    const returnedState = params.get("state");
    const oauthError = params.get("error_description") || params.get("error");

    if (oauthError) {
      setError(oauthError);
      return;
    }
    if (!code) {
      setError("missing authorization code");
      return;
    }
    const expectedState = sessionStorage.getItem("codescout_oauth_state");
    if (expectedState && returnedState && expectedState !== returnedState) {
      setError("state mismatch — please try signing in again");
      return;
    }
    sessionStorage.removeItem("codescout_oauth_state");

    apiFetch(`/api/auth/github/callback?code=${encodeURIComponent(code)}`, { method: "POST" })
      .then(() => router.replace("/repos"))
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "sign in failed, please try again"),
      );
  }, [params, router]);

  if (error) {
    return (
      <div className="text-center">
        <p className="text-red-500">{error}</p>
        <a href="/" className="mt-4 inline-block text-sm underline">
          Back to sign in
        </a>
      </div>
    );
  }

  return <p className="text-black/50 dark:text-white/50">Signing in…</p>;
}

export default function AuthCallbackPage() {
  return (
    <main className="flex flex-1 items-center justify-center px-6 py-24">
      <Suspense fallback={<p className="text-black/50 dark:text-white/50">Signing in…</p>}>
        <CallbackInner />
      </Suspense>
    </main>
  );
}
