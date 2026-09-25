from app.modules.search.events import register_search_outbox, unregister_search_outbox


def startup() -> None:
    register_search_outbox()


def shutdown() -> None:
    unregister_search_outbox()
