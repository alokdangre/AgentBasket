import { NextResponse } from "next/server";

import { clearSessionCookie, getSessionToken } from "@/lib/auth-cookie";

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export async function POST() {
  const token = await getSessionToken();
  if (token) {
    try {
      await fetch(`${commerceApiUrl}/api/v1/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      });
    } catch {
      // Clearing the local cookie is authoritative for this browser session.
    }
  }
  await clearSessionCookie();
  return new NextResponse(null, { status: 204 });
}
