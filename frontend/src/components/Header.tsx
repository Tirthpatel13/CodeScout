"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

export function Header({ user }: { user?: User | null }) {
  const router = useRouter();

  async function signOut() {
    await apiFetch("/api/auth/session", { method: "DELETE" });
    router.push("/");
    router.refresh();
  }

  return (
    <header className="border-b border-black/10 dark:border-white/10">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
        <Link href={user ? "/repos" : "/"} className="flex items-center gap-2 font-semibold">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500" />
          CodeScout
        </Link>
        {user && (
          <div className="flex items-center gap-3 text-sm">
            <img
              src={user.avatar_url}
              alt={user.login}
              className="h-6 w-6 rounded-full"
              referrerPolicy="no-referrer"
            />
            <span className="text-black/70 dark:text-white/70">{user.login}</span>
            <button
              onClick={signOut}
              className="rounded-md border border-black/10 px-2.5 py-1 text-black/60 transition hover:border-black/30 hover:text-black dark:border-white/15 dark:text-white/60 dark:hover:border-white/30 dark:hover:text-white"
            >
              Sign out
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
