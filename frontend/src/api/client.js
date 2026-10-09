// 后端 API 薄封装。开发走 vite proxy，生产同源。
export const BASE = import.meta.env.VITE_API_BASE || "/api";

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch (e) {
      detail = res.statusText;
    }
    throw new Error(res.status + " " + detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  get: (p) => request(p),
  post: (p, body, opts = {}) => request(p, { ...opts, method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: (p, body) => request(p, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  del: (p) => request(p, { method: "DELETE" }),
};
