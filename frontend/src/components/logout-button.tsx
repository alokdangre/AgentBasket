"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { LogoutIcon } from "@/components/icons";

type LogoutButtonProps = {
  className?: string;
  showIcon?: boolean;
};

export function LogoutButton({ className, showIcon = true }: LogoutButtonProps) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function logout() {
    setPending(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      router.push("/");
      router.refresh();
    }
  }

  return (
    <button type="button" className={className} disabled={pending} onClick={logout}>
      {showIcon ? <LogoutIcon /> : null}
      {pending ? "Signing out…" : "Sign out"}
    </button>
  );
}
