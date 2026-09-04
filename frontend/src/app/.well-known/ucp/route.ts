import { type NextRequest, NextResponse } from "next/server";

const commerceApiUrl = (
  process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

export async function GET(request: NextRequest) {
  try {
    const response = await fetch(`${commerceApiUrl}/.well-known/ucp`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2500),
    });
    if (!response.ok) {
      return NextResponse.json(
        { error: "UCP discovery is temporarily unavailable" },
        { status: 502 },
      );
    }
    const profile: unknown = await response.json();
    const etag = response.headers.get("etag");
    const headers = {
      "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
      ...(etag ? { ETag: etag } : {}),
    };
    if (etag && request.headers.get("if-none-match") === etag) {
      return new NextResponse(null, { status: 304, headers });
    }
    return NextResponse.json(profile, {
      headers,
    });
  } catch {
    return NextResponse.json(
      { error: "UCP discovery is temporarily unavailable" },
      { status: 502 },
    );
  }
}
