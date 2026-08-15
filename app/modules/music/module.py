from app.core.module_types import IntegrationSpec, ModuleSpec, UiActionSpec

MODULE = ModuleSpec(
    id="music",
    version="0.1.0",
    title_en="Music",
    title_ru="Музыка",
    dashboard_url="/music/dashboard",
    order=10,
    router="app.modules.music.router:router",
    models="app.modules.music.models",
    tasks="app.modules.music.tasks",
    templates="templates",
    i18n="app.modules.music.i18n",
    file_cleanup="app.modules.music.cleanup:cleanup_file",
    module_cleanup="app.modules.music.cleanup:cleanup_module",
    storage_namespaces=("music",),
    package_prefixes=("song_", "playlist_"),
    package_resolver="app.modules.music.capabilities:resolve_package_resources",
    integrations=(
        IntegrationSpec(
            id="media.audio.import.v1",
            handler="app.modules.music.integrations:import_entity_audio",
            request_model="app.modules.music.integrations:ImportEntityAudioRequest",
            result_model="app.modules.music.integrations:ImportEntityAudioResult",
        ),
    ),
    ui_actions=(
        UiActionSpec(
            id="music.import_audio",
            slot="entity.actions",
            integration="media.audio.import.v1",
            label_en="Send to Music",
            label_ru="Отправить в музыку",
            entity_types=("video",),
        ),
    ),
    progress_key_patterns=("music_dl:*",),
    dependency_extra="music",
    system_packages=("ffmpeg", "nodejs"),
)
