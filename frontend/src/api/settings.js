import { api } from "./client";

// 三个端点返回同一个形状：{ values, defaults, limits }
//   values   —— 当前值（后端已按默认值补齐）
//   defaults —— 默认值，供「恢复默认」按钮用
//   limits   —— 各参数的取值范围，供表单渲染上下界
// 前端不硬编码其中任何一份，理由见 AGENTS.md 硬性约定 #7。

export const getSettings = () => api.get("/settings");
export const patchSettings = (body) => api.patch("/settings", body);
export const resetSettings = () => api.post("/settings/reset", {});
