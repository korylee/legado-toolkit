export const RENAME_BADGES = [
  "ᵃ", "ᵇ", "ᶜ", "ᵈ", "ᵉ", "ᶠ", "ᵍ", "ʰ", "ⁱ", "ʲ", "ᵏ", "ˡ", "ᵐ",
  "ⁿ", "ᵒ", "ᵖ", "ʳ", "ˢ", "ᵗ", "ᵘ", "ᵛ", "ʷ", "ˣ", "ʸ", "ᶻ",
  "⁰", "¹", "²", "³", "⁴", "⁵", "⁶", "⁷", "⁸", "⁹", "⁺", "⁻", "⁼", "⁽", "⁾",
];

export function assignRenameBadges(urls, mode, manualBadges = {}) {
  if (mode === "manual") return urls.map((url) => manualBadges[url] || "");
  return urls.map((_url, index) => RENAME_BADGES[index] || "");
}

export function renamedSourceName(name, suffix, badge) {
  return String(name || "") + String(suffix || "").trim() + String(badge || "");
}
