from sqlalchemy import select

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.core.module_types import IntegrationContext
from app.modules.planner.models import PlannerEvent, PlannerTask

ENTITY_TASK = "planner_task"
ENTITY_EVENT = "planner_event"


def _subtitle(when: str | None, space_name: str | None) -> str | None:
    parts = [part for part in (when, space_name) if part]
    return " · ".join(parts)[:255] or None


def _task_document(task: PlannerTask) -> SearchDocument:
    due = task.due_at.strftime("%d.%m %H:%M") if task.due_at else None
    body_parts = [part for part in (task.notes, task.raw_text) if part]
    return SearchDocument(
        document_id=f"task-{task.id}",
        entity_type=ENTITY_TASK,
        title=task.title[:255],
        subtitle=_subtitle(f"Срок {due}" if due else None, task.space_name),
        body=" ".join(body_parts)[:4000] or None,
        keywords=[item for item in (task.space_name, task.status, task.recurrence) if item][:50],
        updated_at=task.updated_at,
        open_path="/planner",
    )


def _event_document(event: PlannerEvent) -> SearchDocument:
    starts = event.starts_at.strftime("%d.%m %H:%M") if event.starts_at else None
    body_parts = [part for part in (event.notes, event.location, event.raw_text) if part]
    return SearchDocument(
        document_id=f"event-{event.id}",
        entity_type=ENTITY_EVENT,
        title=event.title[:255],
        subtitle=_subtitle(starts, event.space_name),
        body=" ".join(body_parts)[:4000] or None,
        keywords=[item for item in (event.space_name, event.status, event.recurrence) if item][:50],
        updated_at=event.updated_at,
        open_path="/planner",
    )


async def search_documents(
    request: SearchDocumentsRequest,
    context: IntegrationContext,
) -> SearchDocumentsResult:
    user_id = context.user.id
    # Per-table window covers the merged page: a row inside the merged
    # [offset:offset+limit] window sits at most that deep in its own table.
    window = request.offset + request.limit + 1
    tasks = list(
        (
            await context.session.execute(
                select(PlannerTask)
                .where(
                    PlannerTask.user_id == user_id,
                    PlannerTask.status.in_(("todo", "doing", "done")),
                )
                .order_by(PlannerTask.updated_at.desc(), PlannerTask.id.desc())
                .limit(window)
            )
        ).scalars()
    )
    events = list(
        (
            await context.session.execute(
                select(PlannerEvent)
                .where(
                    PlannerEvent.user_id == user_id,
                    PlannerEvent.status.in_(("active", "done")),
                )
                .order_by(PlannerEvent.updated_at.desc(), PlannerEvent.id.desc())
                .limit(window)
            )
        ).scalars()
    )
    merged = sorted(
        [_task_document(task) for task in tasks] + [_event_document(event) for event in events],
        key=lambda doc: (doc.updated_at is not None, doc.updated_at),
        reverse=True,
    )
    page = merged[request.offset : request.offset + request.limit]
    has_more = len(tasks) == window or len(events) == window
    return SearchDocumentsResult(
        module_id="planner",
        documents=page,
        next_offset=request.offset + request.limit if has_more else None,
    )


__all__ = ["search_documents"]
