"""
Jinja2 template engine initialization.

Registers template directories declared by active module manifests alongside
the core templates directory.
"""

from pathlib import Path

from starlette.templating import Jinja2Templates

from app.core.modules import module_registry

# ── Base paths ───────────────────────────────────────────
_CORE_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"  # app/core/templates/


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
    all_dirs.extend(module_registry.template_dirs())

    # Jinja2Templates accepts a single directory or we build a custom loader
    templates = Jinja2Templates(directory=[str(d) for d in all_dirs])

    # Register modular localization context helper
    from app.core.i18n import translate

    templates.env.globals["_"] = translate

    return templates


# Singleton instance — import this from routers
templates = create_templates()
