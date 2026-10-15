"""Book-source name suffix and badge assignment rules shared with the UI contract tests."""

RENAME_BADGES = (
    "ᵃ", "ᵇ", "ᶜ", "ᵈ", "ᵉ", "ᶠ", "ᵍ", "ʰ", "ⁱ", "ʲ", "ᵏ", "ˡ", "ᵐ",
    "ⁿ", "ᵒ", "ᵖ", "ʳ", "ˢ", "ᵗ", "ᵘ", "ᵛ", "ʷ", "ˣ", "ʸ", "ᶻ",
    "⁰", "¹", "²", "³", "⁴", "⁵", "⁶", "⁷", "⁸", "⁹", "⁺", "⁻", "⁼", "⁽", "⁾",
)


def assign_rename_badges(urls, mode, manual_badges=None):
    manual_badges = manual_badges or {}
    if mode == "manual":
        return [manual_badges.get(url) or "" for url in urls]
    return [RENAME_BADGES[index] if index < len(RENAME_BADGES) else "" for index, _url in enumerate(urls)]


def renamed_source_name(name, suffix, badge):
    return str(name or "") + str(suffix or "").strip() + str(badge or "")
