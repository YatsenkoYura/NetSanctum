"""
NetSanctum — Modular Localization (i18n) Subsystem.

Loads translations declared by active module manifests.
Provides the Jinja2 context translator helper.
"""

import importlib
import logging

from jinja2 import pass_context

from app.core.modules import module_registry

logger = logging.getLogger(__name__)

# Memory cache for all module translations
# Structure: { "module_name": { "en": { "key": "val" }, "ru": { "key": "val" } } }
_TRANSLATIONS: dict[str, dict[str, dict[str, str]]] = {}


def discover_and_load_translations():
    """
    Import translation modules declared by active manifests.
    Caches translations in-memory.
    """
    _TRANSLATIONS.clear()

    for module_name, i18n_module_path in module_registry.translation_modules():
        try:
            mod = importlib.import_module(i18n_module_path)
            if hasattr(mod, "TRANSLATIONS"):
                _TRANSLATIONS[module_name] = mod.TRANSLATIONS
                logger.info("Loaded translations for module: %s", module_name)
            else:
                logger.warning("Module %s has no 'TRANSLATIONS' attribute", module_name)
        except Exception as e:
            logger.error("Failed to load translations for module %s: %s", module_name, e)


# Initialize translations cache on startup
discover_and_load_translations()


@pass_context
def translate(context, module: str, key: str, **kwargs) -> str:
    """
    Jinja2 translation helper function.
    Usage in templates: {{ _('auth', 'access_terminal') }}

    Resolution order:
    1. Context variables: `context.get("lang")` (e.g. explicitly passed to template).
    2. Cookie check: `request.cookies.get("lang")`.
    3. Fallback default: "en".

    Module translation lookup:
    1. If the module has translations for the target language, use it.
    2. If the module does not have that language, fall back to the first available language dictionary.
    3. If the key is still missing, return the raw key name to avoid rendering crash.
    """
    # 1. Determine active language
    lang = context.get("lang")
    if not lang:
        request = context.get("request")
        if request:
            lang = request.cookies.get("lang")

    if not lang:
        lang = "en"

    lang = lang.lower()

    # 2. Get module dictionary
    module_dict = _TRANSLATIONS.get(module)
    if not module_dict:
        # Fallback if module has no i18n file at all
        return key

    # 3. Retrieve language dictionary (with first-available fallback)
    lang_dict = module_dict.get(lang)
    if not lang_dict:
        # Fulfills requirement: "если нет [языка], то на первом что есть"
        available_languages = list(module_dict.keys())
        if available_languages:
            first_lang = available_languages[0]
            lang_dict = module_dict[first_lang]
        else:
            return key

    # 4. Lookup key
    value = lang_dict.get(key)
    if value is None:
        return key

    # 5. Format string if placeholders are provided
    if kwargs:
        try:
            return value.format(**kwargs)
        except Exception:
            pass

    return value
