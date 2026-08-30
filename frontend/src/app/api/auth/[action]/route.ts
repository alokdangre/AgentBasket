import { NextRequest, NextResponse } from "next/server";

import type { ApiError, SessionUser } from "@/lib/account-types";
import { setSessionCookie } from "@/lib/auth-cookie";

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

type AuthPayload = {
  access_token: string;
  expires_at: string;
  user: SessionUser;
};

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ action: string }> },
) {
  const { action } = await context.params;
  if (action !== "login" && action !== "register") {
    return NextResponse.json({ error: { message: "Not found" } }, { status: 404 });
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: { message: "Invalid JSON body" } }, { status: 400 });
  }
  try {
    const response = await fetch(`${commerceApiUrl}/api/v1/auth/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const payload = (await response.json()) as AuthPayload | ApiError;
    if (!response.ok || !("access_token" in payload)) {
      return NextResponse.json(payload, { status: response.status });
    }
    await setSessionCookie(payload.access_token, payload.expires_at);
    return NextResponse.json({ user: payload.user }, { status: response.status });
  } catch {
    return NextResponse.json(
      { error: { message: "The commerce service is unavailable. Try again shortly." } },
      { status: 503 },
    );
  }
}
