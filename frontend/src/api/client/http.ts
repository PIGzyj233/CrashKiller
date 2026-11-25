export const API_BASE = "/api/v1";

// ============================================================================
// Error Handling
// ============================================================================

export interface FastAPIValidationErrorItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function formatValidationItem(item: unknown): string {
  if (typeof item === "object" && item !== null && "msg" in item) {
    const typed = item as FastAPIValidationErrorItem;
    const loc =
      Array.isArray(typed.loc) && typed.loc.length > 0
        ? `${typed.loc.join(".")}: `
        : "";
    return `${loc}${typed.msg ?? "Validation error"}`;
  }
  return JSON.stringify(item);
}

export function parseErrorPayload(
  status: number,
  statusText: string,
  rawText: string,
): { message: string; detail: unknown } {
  let parsed: unknown = undefined;
  if (rawText.trim()) {
    try {
      parsed = JSON.parse(rawText) as unknown;
    } catch {
      return {
        message: rawText.trim().slice(0, 300),
        detail: rawText,
      };
    }
  }

  if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return { message: detail, detail };
    }
    if (Array.isArray(detail)) {
      return {
        message: detail.map(formatValidationItem).join("; "),
        detail,
      };
    }
    return {
      message: JSON.stringify(detail),
      detail,
    };
  }

  if (rawText.trim()) {
    return {
      message: rawText.trim().slice(0, 300),
      detail: parsed ?? rawText,
    };
  }

  return {
    message: `HTTP ${status} ${statusText}`.trim(),
    detail: undefined,
  };
}

// ============================================================================
// Query String & JSON Request Helpers
// ============================================================================

export function buildQueryString(params?: object): string {
  if (!params) {
    return "";
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export async function requestJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    ...init,
    headers,
  });

  if (!response.ok) {
    const rawText = await response.text();
    const { message, detail } = parseErrorPayload(
      response.status,
      response.statusText,
      rawText,
    );
    throw new ApiError(response.status, message, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

// ============================================================================
// Internal XHR Multipart Helper
// ============================================================================

export interface MultipartXhrOptions {
  url: string;
  formData: FormData;
  apiKey?: string;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

export function sendMultipartXhr<T>(options: MultipartXhrOptions): Promise<T> {
  const { url, formData, apiKey, onProgress, signal } = options;

  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError(0, "Upload aborted"));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Accept", "application/json");
    if (apiKey) {
      xhr.setRequestHeader("X-API-Key", apiKey);
    }

    const handleAbortSignal = () => {
      xhr.abort();
    };

    if (signal) {
      signal.addEventListener("abort", handleAbortSignal, { once: true });
    }

    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && ev.total > 0 && onProgress) {
        const percent = Math.min(
          100,
          Math.round((ev.loaded / ev.total) * 100),
        );
        onProgress(percent);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T);
        } catch {
          reject(
            new ApiError(
              xhr.status,
              "Invalid JSON response from server",
              xhr.responseText,
            ),
          );
        }
        return;
      }

      const { message, detail } = parseErrorPayload(
        xhr.status,
        xhr.statusText,
        xhr.responseText,
      );
      reject(new ApiError(xhr.status, message, detail));
    };

    xhr.onerror = () => {
      reject(new ApiError(0, "Network error during upload"));
    };

    xhr.onabort = () => {
      reject(new ApiError(0, "Upload aborted"));
    };

    xhr.onloadend = () => {
      if (signal) {
        signal.removeEventListener("abort", handleAbortSignal);
      }
    };

    xhr.send(formData);
  });
}
