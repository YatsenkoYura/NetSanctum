from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="computercraft",
    version="0.1.0",
    title_en="ComputerCraft",
    title_ru="ComputerCraft",
    order=60,
    router="app.modules.computercraft.router:router",
    uses_integration_contracts=("library.viewer.v1",),
    system_packages=("ffmpeg",),
)
