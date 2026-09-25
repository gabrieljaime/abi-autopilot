# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
"""Authorship, license and disclaimer.

The single source of this text: used by the CLI and by the footer of every
comment Autopilot posts on GitHub.
"""
from __future__ import annotations

from . import __version__

PRODUCT = "ABI Autopilot"
COPYRIGHT = "Copyright (c) 2026 Gabriel Jaime"
LICENSE = "MIT"

DISCLAIMER = (
    "Changes are produced by AI agents and must be reviewed before deployment. "
    "The software is provided \"as is\", without warranty of any kind; the "
    "authors are not liable for any damage arising from its use."
)

ACCOUNTS = (
    "Requires your own Claude account (always, for review) and optionally an "
    "OpenAI Codex account, via subscription or API key; every agent call uses "
    "your quota or credit."
)

# Stable marker so a footer is never appended twice.
FOOTER_MARKER = "<!-- abi-autopilot-footer -->"


def cli_notice() -> str:
    return (
        f"{PRODUCT} v{__version__} · {COPYRIGHT} · {LICENSE} License\n"
        f"{DISCLAIMER}\n{ACCOUNTS}"
    )


def comment_footer() -> str:
    return (
        f"\n\n---\n{FOOTER_MARKER}\n"
        f"<sub>Generated automatically by {PRODUCT} v{__version__} · "
        f"{COPYRIGHT} · {LICENSE} License.<br>{DISCLAIMER}</sub>"
    )


def with_footer(body: str) -> str:
    if FOOTER_MARKER in body:
        return body
    return body.rstrip() + comment_footer()
