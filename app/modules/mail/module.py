from app.core.module_types import MigrationSpec, ModuleSpec

MODULE = ModuleSpec(
    id="mail",
    version="0.1.0",
    title_en="Mail",
    title_ru="Почта",
    dashboard_url="/mail",
    order=40,
    router="app.modules.mail.router:router",
    models="app.modules.mail.models",
    migrations=MigrationSpec(
        path="migrations",
        baseline_revision="mail_0001",
        tables=("mail_folders", "mail_messages", "mail_contacts", "mail_accounts", "mail_attachments"),
    ),
    templates="templates",
    bundled=False,
)
