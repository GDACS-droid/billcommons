import { API_BASE } from "./config";

/**
 * Result wrapper so every page can render an honest "data unavailable"
 * state instead of throwing (the API may be down; see BRIEF-wave2.md).
 */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number };

export async function apiGet<T>(
  path: string,
  searchParams?: Record<string, string | number | undefined>
): Promise<ApiResult<T>> {
  const url = new URL(path.replace(/^\//, ""), `${API_BASE}/`);
  if (searchParams) {
    for (const [key, value] of Object.entries(searchParams)) {
      if (value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }

  try {
    const res = await fetch(url.toString(), {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });

    if (!res.ok) {
      return {
        ok: false,
        error: `API returned ${res.status} ${res.statusText}`,
        status: res.status,
      };
    }

    const data = (await res.json()) as T;
    return { ok: true, data };
  } catch (err) {
    return {
      ok: false,
      error:
        err instanceof Error
          ? `Could not reach the API: ${err.message}`
          : "Could not reach the API.",
    };
  }
}
