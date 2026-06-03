"""
Jinja2 template engine initialization.

Scans app/modules/*/templates/ directories and registers them alongside
the core templates directory so that each module's HTML fragments are
discoverable by the unified Jinja2Templates instance.
"""

import pkgutil
from pathlib import Path

from starlette.templating import Jinja2Templates

# ── Base paths ───────────────────────────────────────────
_APP_DIR = Path(__file__).resolve().parent.parent          # app/
_CORE_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"  # app/core/templates/
_MODULES_DIR = _APP_DIR / "modules"


def _discover_template_dirs() -> list[Path]:
    """
    Walk app/modules/ and collect every sub-directory named 'templates'.
    Returns them sorted alphabetically for deterministic ordering.
    """
    dirs: list[Path] = []
    if not _MODULES_DIR.is_dir():
        return dirs

    for child in sorted(_MODULES_DIR.iterdir()):
        if child.is_dir():
            tpl_dir = child / "templates"
            if tpl_dir.is_dir():
                dirs.append(tpl_dir)

    return dirs


def create_templates() -> Jinja2Templates:
    """
    Build a Jinja2Templates instance that searches:
      1. app/core/templates/          (base layouts, shared partials)
      2. app/modules/<name>/templates (per-module fragments)
    """
    # Ensure core templates dir exists
    _CORE_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all template directories: core first, then modules
    all_dirs: list[Path] = [_CORE_TEMPLATES_DIR]
    all_dirs.extend(_discover_template_dirs())

    # Jinja2Templates accepts a single directory or we build a custom loader
    templates = Jinja2Templates(directory=[str(d) for d in all_dirs])

    # Register modular localization context helper
    from app.core.i18n import translate
    templates.env.globals["_"] = translate

    return templates


# Singleton instance — import this from routers
templates = create_templates()

