"""Brand palette + ASCII art used across the installer."""
from __future__ import annotations

# Azure Linux brand colors.
AZURE_BLUE = "#0078d4"      # primary brand
AZURE_BLUE_DEEP = "#005a9e"  # darker tone for surfaces
AZURE_NAVY = "#0a1929"       # background
AZURE_SURFACE = "#1a2937"    # card backgrounds
AZURE_CYAN = "#50e6ff"       # accent / highlights
AZURE_WHITE = "#f2f5f8"
AZURE_DIM = "#7a8a9a"
AZURE_OK = "#36c98c"
AZURE_WARN = "#f7b500"
AZURE_ERR = "#e74856"


# Solid-block banner using ONLY U+2588 FULL BLOCK and spaces. The kernel
# framebuffer font (lat15-fixed16) includes the full-block glyph; we
# deliberately avoid the heavy box-drawing glyphs (╔ ╗ ╝ ╚ ║ ═) and the
# half-blocks (▀ ▄ ▌ ▐) used by ANSI-Shadow-style fonts, since those
# don't all exist in this font and render as broken-square placeholders.
BANNER = (
    "  ██   ██████ ██  ██ █████  ██████      ██     ██████ ██  ██ ██  ██ ██  ██\n"
    " ████      ██ ██  ██ ██  ██ ██          ██       ██   ███ ██ ██  ██  ████ \n"
    "██  ██   ██   ██  ██ █████  █████       ██       ██   ██████ ██  ██   ██  \n"
    "██████ ██     ██  ██ ██ ██  ██          ██       ██   ██ ███ ██  ██  ████ \n"
    "██  ██ ██████  ████  ██  ██ ██████      ██████ ██████ ██  ██  ████  ██  ██\n"
)

SMALL_BANNER = "Azure Linux 4.0 - Installer"
