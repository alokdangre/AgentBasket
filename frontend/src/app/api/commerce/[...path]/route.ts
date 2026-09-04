import { NextRequest, NextResponse } from "next/server";

import { getSessionToken } from "@/lib/auth-cookie";

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

function isAllowed(method: string, path: string[]): boolean {
  const exactRoutes = new Set([
    "GET me",
    "PATCH me",
    "GET me/addresses",
    "POST me/addresses",
    "GET me/passkeys",
    "POST me/passkeys/registration/options",
    "POST me/passkeys/registration/verify",
    "GET credential-provider/payment-instruments",
    "GET scheduled-purchases",
    "POST scheduled-purchases",
    "GET cart",
    "POST cart/items",
    "POST checkouts/from-cart",
    "GET orders",
    "POST payments/razorpay/verify",
    "POST agent/conversations",
    "POST agent/conversations/current",
    "GET merchant/operations/dashboard",
    "GET merchant/operations/orders",
    "GET merchant/operations/inventory",
  ]);
  const route = `${method} ${path.join("/")}`;
  if (exactRoutes.has(route)) return true;
  if (
    path.length === 3 &&
    path[0] === "me" &&
    path[1] === "addresses" &&
    ["PATCH", "DELETE"].includes(method)
  ) {
    return true;
  }
  if (
    path.length === 4 &&
    path[0] === "checkouts" &&
    path[2] === "ap2" &&
    ["challenge", "approve"].includes(path[3]) &&
    method === "POST"
  ) {
    return true;
  }
  if (
    path.length === 4 &&
    path[0] === "credential-provider" &&
    path[1] === "razorpay-upi-autopay" &&
    ["session", "verify"].includes(path[3]) &&
    method === "POST"
  ) {
    return true;
  }
  if (
    path.length === 4 &&
    path[0] === "scheduled-purchases" &&
    path[2] === "authorization" &&
    ["challenge", "approve"].includes(path[3]) &&
    method === "POST"
  ) {
    return true;
  }
  if (
    path.length === 3 &&
    path[0] === "scheduled-purchases" &&
    ["pause", "resume", "revoke", "run-now"].includes(path[2]) &&
    method === "POST"
  ) {
    return true;
  }
  if (
    path.length === 4 &&
    path[0] === "checkouts" &&
    path[2] === "ap2" &&
    path[3] === "evidence" &&
    method === "GET"
  ) {
    return true;
  }
  if (path.length === 3 && path[0] === "cart" && path[1] === "items") {
    return ["PATCH", "DELETE"].includes(method);
  }
  if (path.length === 2 && path[0] === "orders" && method === "GET") return true;
  if (path.length === 2 && path[0] === "checkouts" && method === "GET") return true;
  if (
    path.length === 3 &&
    path[0] === "agent" &&
    path[1] === "conversations" &&
    method === "GET"
  ) {
    return true;
  }
  if (
    path.length === 4 &&
    path[0] === "agent" &&
    path[1] === "conversations" &&
    path[3] === "messages" &&
    method === "POST"
  ) {
    return true;
  }
  if (
    path.length === 3 &&
    path[0] === "checkouts" &&
    ["approve", "payment-session", "cancel"].includes(path[2]) &&
    method === "POST"
  ) {
    return true;
  }
  return (
    path.length === 4 &&
    path[0] === "merchant" &&
    path[1] === "operations" &&
    ["orders", "inventory"].includes(path[2]) &&
    method === "PATCH"
  );
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const path = (await context.params).path;
  if (!isAllowed(request.method, path)) {
    return NextResponse.json({ error: { message: "Not found" } }, { status: 404 });
  }
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json(
      { error: { code: "authentication_required", message: "Sign in to continue." } },
      { status: 401 },
    );
  }
  const target = new URL(`${commerceApiUrl}/api/v1/${path.map(encodeURIComponent).join("/")}`);
  target.search = request.nextUrl.search;
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const idempotencyKey = request.headers.get("Idempotency-Key");
  const isLongRunningRequest =
    path[0] === "agent" || (path[0] === "scheduled-purchases" && path[2] === "run-now");
  const timeoutMs = isLongRunningRequest ? 40000 : 8000;
  try {
    const response = await fetch(target, {
      method: request.method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
      },
      body: hasBody ? await request.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 204) return new NextResponse(null, { status: 204 });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json" },
    });
  } catch {
    return NextResponse.json(
      { error: { message: "The commerce service is unavailable. Try again shortly." } },
      { status: 503 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
