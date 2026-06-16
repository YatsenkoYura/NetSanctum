"""generalize_lib_models

Revision ID: 8c67bff5bfcb
Revises: 4ee3ae65250e
Create Date: 2026-06-14 16:27:35.536112

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8c67bff5bfcb'
down_revision: Union[str, None] = '4ee3ae65250e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema by renaming tables and adding new fields."""
    # 1. Drop obsolete tables if they exist
    for t in ['sucub_playlist_association', 'sucub_playlists', 'sucub_items']:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")

    # 2. Rename ranobe_novels to lib_media
    op.rename_table('ranobe_novels', 'lib_media')

    # 3. Add new columns to lib_media
    op.add_column('lib_media', sa.Column('site_id', sa.Integer(), nullable=False, server_default='3'))
    op.add_column('lib_media', sa.Column('media_type', sa.String(length=50), nullable=False, server_default='novel'))
    # Remove server default after setting it for existing rows
    op.alter_column('lib_media', 'site_id', server_default=None)
    op.alter_column('lib_media', 'media_type', server_default=None)

    # 4. Rename ranobe_chapters to lib_chapters
    op.rename_table('ranobe_chapters', 'lib_chapters')

    # 5. Rename column novel_id to media_id in lib_chapters
    op.alter_column('lib_chapters', 'novel_id', new_column_name='media_id')

    # 6. Re-create foreign key for lib_chapters -> lib_media
    op.drop_constraint('ranobe_chapters_novel_id_fkey', 'lib_chapters', type_='foreignkey')
    op.create_foreign_key(
        'lib_chapters_media_id_fkey',
        'lib_chapters', 'lib_media',
        ['media_id'], ['id'],
        ondelete='CASCADE'
    )

    # 7. Add new columns to lib_chapters
    op.add_column('lib_chapters', sa.Column('pages_list', sa.JSON(), nullable=True))
    op.add_column('lib_chapters', sa.Column('video_path', sa.String(length=510), nullable=True))

    # 8. Adjust archived_videos subtitles if needed
    op.execute("ALTER TABLE archived_videos ALTER COLUMN subtitles TYPE JSON USING subtitles::json;")


def downgrade() -> None:
    """Downgrade schema back to ranobe_* specific tables."""
    # 1. Revert subtitles type
    op.execute("ALTER TABLE archived_videos ALTER COLUMN subtitles TYPE JSONB USING subtitles::jsonb;")

    # 2. Drop columns from lib_chapters
    op.drop_column('lib_chapters', 'video_path')
    op.drop_column('lib_chapters', 'pages_list')

    # 3. Revert foreign key
    op.drop_constraint('lib_chapters_media_id_fkey', 'lib_chapters', type_='foreignkey')
    op.create_foreign_key(
        'ranobe_chapters_novel_id_fkey',
        'lib_chapters', 'lib_media',
        ['media_id'], ['id'],
        ondelete='CASCADE'
    )

    # 4. Rename column back to novel_id
    op.alter_column('lib_chapters', 'media_id', new_column_name='novel_id')

    # 5. Rename table back to ranobe_chapters
    op.rename_table('lib_chapters', 'ranobe_chapters')

    # 6. Drop columns from lib_media
    op.drop_column('lib_media', 'media_type')
    op.drop_column('lib_media', 'site_id')

    # 7. Rename table back to ranobe_novels
    op.rename_table('lib_media', 'ranobe_novels')
