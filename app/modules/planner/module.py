from app.core.module_types import (
    IntegrationEffect,
    IntegrationEffects,
    IntegrationSpec,
    MigrationSpec,
    ModuleSpec,
)

MODULE = ModuleSpec(
    id="planner",
    version="0.1.0",
    title_en="Planner",
    title_ru="Планировщик",
    dashboard_url="/planner",
    order=35,
    router="app.modules.planner.router:router",
    models="app.modules.planner.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="planner_0001",
        tables=("planner_task", "planner_event"),
    ),
    templates="templates",
    tasks="app.modules.planner.tasks",
    # Plans live in Vault spaces, so the module consumes the spaces listing.
    uses_integrations=("vault.spaces.v1",),
    integrations=(
        IntegrationSpec(
            id="planner.create_task.v1",
            handler="app.modules.planner.integrations:create_task",
            request_model="app.contracts.planner_v1:PlannerCreateTaskRequest",
            result_model="app.contracts.planner_v1:PlannerCreateTaskResult",
            description=(
                "Create a dated to-do. Send due_at as ISO datetime with offset; omit space "
                "for the inbox. Unknown space or bad date returns invalid with a hint."
            ),
            effects=IntegrationEffects(
                effect=IntegrationEffect.CREATE,
                reversible=True,
                undo_integration="planner.undo.v1",
            ),
        ),
        IntegrationSpec(
            id="planner.list_tasks.v1",
            handler="app.modules.planner.integrations:list_tasks",
            request_model="app.contracts.planner_v1:PlannerListTasksRequest",
            result_model="app.contracts.planner_v1:PlannerListTasksResult",
            description=(
                "List the owner's tasks: today, overdue, upcoming, or all, "
                "optionally filtered by Vault space name."
            ),
            effects=IntegrationEffects(effect=IntegrationEffect.READ, idempotent=True),
        ),
        IntegrationSpec(
            id="planner.complete_task.v1",
            handler="app.modules.planner.integrations:complete_task",
            request_model="app.contracts.planner_v1:PlannerCompleteTaskRequest",
            result_model="app.contracts.planner_v1:PlannerCompleteTaskResult",
            description=("Close a task by id. A repeating task spawns its next instance automatically."),
            effects=IntegrationEffects(
                effect=IntegrationEffect.UPDATE,
                reversible=True,
                undo_integration="planner.undo.v1",
            ),
        ),
        IntegrationSpec(
            id="planner.snooze_task.v1",
            handler="app.modules.planner.integrations:snooze_task",
            request_model="app.contracts.planner_v1:PlannerSnoozeTaskRequest",
            result_model="app.contracts.planner_v1:PlannerSnoozeTaskResult",
            description="Move a task's reminder push to a new ISO datetime.",
            effects=IntegrationEffects(effect=IntegrationEffect.UPDATE, idempotent=True),
        ),
        IntegrationSpec(
            id="planner.create_event.v1",
            handler="app.modules.planner.integrations:create_event",
            request_model="app.contracts.planner_v1:PlannerCreateEventRequest",
            result_model="app.contracts.planner_v1:PlannerCreateEventResult",
            description=(
                "Create a calendar event. starts_at is required ISO datetime with offset; "
                "omit space for the inbox."
            ),
            effects=IntegrationEffects(
                effect=IntegrationEffect.CREATE,
                reversible=True,
                undo_integration="planner.undo.v1",
            ),
        ),
        IntegrationSpec(
            id="planner.list_events.v1",
            handler="app.modules.planner.integrations:list_events",
            request_model="app.contracts.planner_v1:PlannerListEventsRequest",
            result_model="app.contracts.planner_v1:PlannerListEventsResult",
            description="List upcoming calendar events for the next N days.",
            effects=IntegrationEffects(effect=IntegrationEffect.READ, idempotent=True),
        ),
        IntegrationSpec(
            id="planner.today.v1",
            handler="app.modules.planner.integrations:today",
            request_model="app.contracts.planner_v1:PlannerTodayRequest",
            result_model="app.contracts.planner_v1:PlannerTodayResult",
            description=(
                "Today's agenda: overdue first, then due today, then events. "
                "If it lists overdue items or something due within 2 hours and the user "
                "did not ask about plans, mention at most one line at the end, otherwise "
                "stay silent about it."
            ),
            effects=IntegrationEffects(effect=IntegrationEffect.READ, idempotent=True),
        ),
        IntegrationSpec(
            id="planner.resolve_space.v1",
            handler="app.modules.planner.integrations:resolve_space",
            request_model="app.contracts.planner_v1:PlannerResolveSpaceRequest",
            result_model="app.contracts.planner_v1:PlannerResolveSpaceResult",
            description="Resolve a spoken Vault space name to its id before filtering by space.",
            effects=IntegrationEffects(effect=IntegrationEffect.READ, idempotent=True),
        ),
        IntegrationSpec(
            id="planner.undo.v1",
            handler="app.modules.planner.integrations:undo_planner_write",
            request_model="app.contracts.undo_v1:UndoRequest",
            result_model="app.contracts.undo_v1:UndoResult",
            description="Reverse a planner create or complete addressed by its arguments.",
            contract="undo.v1",
            effects=IntegrationEffects(
                effect=IntegrationEffect.DELETE,
                idempotent=True,
            ),
        ),
    ),
)
