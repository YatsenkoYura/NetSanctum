from app.core.module_types import (
    IntegrationEffect,
    IntegrationEffects,
    IntegrationSpec,
    MigrationSpec,
    ModuleSpec,
)

MODULE = ModuleSpec(
    id="miku",
    version="0.6.0",
    title_en="MIKU",
    title_ru="MIKU",
    dashboard_url="/miku/dashboard",
    order=5,
    router="app.modules.miku.router:router",
    models="app.modules.miku.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="miku_0001",
        tables=(
            "miku_turn_audit",
            "miku_profile_memory",
            "miku_episode_memory",
            "miku_cascade_log",
            "miku_task",
            "miku_conversation",
            "miku_conversation_message",
            "miku_conversation_note",
        ),
    ),
    templates="templates",
    tasks="app.modules.miku.tasks",
    # Memory is a tool, not a command: the model may store and forget on its own.
    integrations=(
        IntegrationSpec(
            id="miku.memory.write.v1",
            handler="app.modules.miku.integrations:write_memory",
            request_model="app.contracts.miku_memory_v1:MikuMemoryWriteRequest",
            result_model="app.contracts.miku_memory_v1:MikuMemoryWriteResult",
            description=(
                "Store or remove one long-lived memory item. Scope profile keeps a keyed fact; "
                "scope episodic keeps an event summary. Operation delete forgets it."
            ),
            effects=IntegrationEffects(
                effect=IntegrationEffect.UPDATE,
                external_io=False,
                idempotent=False,
                reversible=True,
                undo_integration="miku.memory.undo.v1",
            ),
        ),
        IntegrationSpec(
            id="miku.memory.undo.v1",
            handler="app.modules.miku.integrations:undo_memory_write",
            request_model="app.contracts.undo_v1:UndoRequest",
            result_model="app.contracts.undo_v1:UndoResult",
            description="Forget one memory item that a previous write added.",
            contract="undo.v1",
            effects=IntegrationEffects(
                effect=IntegrationEffect.DELETE,
                external_io=False,
                idempotent=True,
            ),
        ),
        IntegrationSpec(
            id="miku.memory.search.v1",
            handler="app.modules.miku.integrations:search_memory",
            request_model="app.contracts.miku_memory_v1:MikuMemorySearchRequest",
            result_model="app.contracts.miku_memory_v1:MikuMemorySearchResult",
            description="Recall stored memory items and episode summaries, newest first.",
            effects=IntegrationEffects(
                effect=IntegrationEffect.READ,
                external_io=False,
                idempotent=True,
            ),
        ),
        # What still matters inside the current conversation. The model only sees a
        # short window of turns, so this is how it carries the rest forward itself.
        IntegrationSpec(
            id="miku.conversation.note.write.v1",
            handler="app.modules.miku.integrations:write_note",
            request_model="app.contracts.miku_conversation_note_v1:MikuNoteWriteRequest",
            result_model="app.contracts.miku_conversation_note_v1:MikuNoteWriteResult",
            description=(
                "Keep one note in the current conversation, or drop it. Use it to carry "
                "forward what still matters: the subject being discussed, a preference the "
                "user stated, a decision already made."
            ),
            effects=IntegrationEffects(
                effect=IntegrationEffect.UPDATE,
                external_io=False,
                idempotent=False,
                reversible=True,
                undo_integration="miku.conversation.note.undo.v1",
            ),
        ),
        IntegrationSpec(
            id="miku.conversation.note.search.v1",
            handler="app.modules.miku.integrations:search_notes",
            request_model="app.contracts.miku_conversation_note_v1:MikuNoteSearchRequest",
            result_model="app.contracts.miku_conversation_note_v1:MikuNoteSearchResult",
            description="Read back the notes kept in the current conversation.",
            effects=IntegrationEffects(
                effect=IntegrationEffect.READ,
                external_io=False,
                idempotent=True,
            ),
        ),
        IntegrationSpec(
            id="miku.conversation.note.undo.v1",
            handler="app.modules.miku.integrations:undo_note_write",
            request_model="app.contracts.undo_v1:UndoRequest",
            result_model="app.contracts.undo_v1:UndoResult",
            description="Forget one note that a previous write added to this conversation.",
            contract="undo.v1",
            effects=IntegrationEffects(
                effect=IntegrationEffect.DELETE,
                external_io=False,
                idempotent=True,
            ),
        ),
    ),
    uses_integrations=(
        "media.video.archive.v1",
        "miku.conversation.note.search.v1",
        "miku.conversation.note.write.v1",
        "miku.memory.search.v1",
        "miku.memory.undo.v1",
        "miku.memory.write.v1",
        "search.global.v1",
        "vault.capture.v1",
    ),
    # library.viewer stays declared for server-side resource resolution only.
    # runtime_tools() hides it from the model so discovery always goes via search.global.v1.
    uses_integration_contracts=("library.viewer.v1", "video.source.catalog.v1"),
)
