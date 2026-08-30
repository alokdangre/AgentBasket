import "server-only";

import { cache } from "react";

import type { SessionUser, UserRole } from "@/lib/account-types";
import { getSessionToken } from "@/lib/auth-cookie";

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export async function authorizedFetch<T>(path: string): Promise<T | null> {
  const token = await getSessionToken();
  if (!token) return null;
  try {
    const response = await fetch(`${commerceApiUrl}/api/v1/${path.replace(/^\//, "")}`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export const getSessionUser = cache(async () => authorizedFetch<SessionUser>("me"));

export function hasMerchantRole(user: SessionUser | null): user is SessionUser {
  const roles: UserRole[] = ["merchant_admin", "merchant_staff"];
  return Boolean(user && roles.includes(user.role));
}
