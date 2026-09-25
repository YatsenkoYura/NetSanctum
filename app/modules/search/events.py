from sqlalchemy import event
from sqlalchemy.orm import Session

from app.modules.search.models import SearchRefreshOutbox

INDEXED_TABLE_MODULES = {
    "songs": "music",
    "archived_videos": "video_archiver",
    "lib_media": "alllib",
    "lib_chapters": "alllib",
    "vault_items": "vault",
}
_registered = False


def register_search_outbox() -> None:
    global _registered
    if _registered:
        return
    event.listen(Session, "before_flush", _enqueue_changed_modules)
    _registered = True


def unregister_search_outbox() -> None:
    global _registered
    if not _registered:
        return
    event.remove(Session, "before_flush", _enqueue_changed_modules)
    _registered = False


def _enqueue_changed_modules(session: Session, _flush_context, _instances) -> None:
    changed_modules = {
        module_id
        for instance in (*session.new, *session.dirty, *session.deleted)
        if (table := getattr(instance, "__table__", None)) is not None
        and (module_id := INDEXED_TABLE_MODULES.get(table.name))
    }
    queued_modules = {item.source_module_id for item in session.new if isinstance(item, SearchRefreshOutbox)}
    for module_id in changed_modules - queued_modules:
        session.add(SearchRefreshOutbox(source_module_id=module_id))
