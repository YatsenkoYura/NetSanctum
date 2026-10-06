"""
Jinja2 template engine initialization.

Registers template directories declared by active module manifests alongside
the core templates directory.
"""

from pathlib import Path

from starlette.templating import Jinja2Templates

from app.core.modules import module_registry
from app.core.template_globals import DEFERRED_GLOBALS

# ── Base paths ───────────────────────────────────────────
_CORE_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"  # app/core/templates/


def deferred_globals(_request) -> dict:
    """What modules published, as this render's context.

    A copy, not the dict itself: a template is not a place to mutate the values
    every later render will start from.
    """
    return dict(DEFERRED_GLOBALS)


def register_globals(environment) -> None:
    """Give a Jinja environment the globals every application template may use.

    The nonce matters most: a template renders `<script nonce="...">` only when it
    has one, so a page whose policy demands a nonce breaks without this rather than
    degrading — which is the correct direction for a security-relevant value.
    """
    from app.core.http_security import csp_nonce
    from app.core.i18n import translate

    environment.globals["_"] = translate
    environment.globals["csp_nonce"] = csp_nonce


def create_templates() -> Jinja2Templates:
    """
    Build a Jinja2Templates instance that searches:
      1. app/core/templates/          (base layouts, shared partials)
      2. app/modules/<name>/templates (per-module fragments)

    A context processor merges whatever modules have published into every
    render. It is a processor rather than a set of `globals` because a module is
    imported *after* this engine is built, and a global captured here would
    freeze the value before discovery had finished — so a template that reads a
    module's contribution has to be answered at render time.
    """
    # Ensure core templates dir exists
    _CORE_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all template directories: core first, then modules
    all_dirs: list[Path] = [_CORE_TEMPLATES_DIR]
    all_dirs.extend(module_registry.template_dirs())

    # Jinja2Templates accepts a single directory or we build a custom loader
    templates = Jinja2Templates(directory=[str(d) for d in all_dirs], context_processors=[deferred_globals])

    register_globals(templates.env)

    return templates


# Singleton instance — import this from routers
templates = create_templates()
