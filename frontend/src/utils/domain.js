// URL → 展示用域名。三处共用（导入预览表、仓库卡片、仓库预览抽屉）——
// 解析失败的兜底也必须同一份：一处显示原始串、一处显示「域名未知」会让
// 同一条源在两个面板里长得不一样。

/** URL → host；解析不了原样返回，空值返回「域名未知」。 */
export function domainOf(url) {
    const value = String(url || "");
    try {
        return new URL(value).host || value || "域名未知";
    } catch {
        return value || "域名未知";
    }
}
