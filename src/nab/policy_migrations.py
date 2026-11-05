"""Replacements for distribution policies that mixed installation and metadata."""

DIST_POLICY_REPLACEMENTS = {
    "prefer-wheel": "wheel-or-sdist",
    "sdist-install": "sdist-only",
}
