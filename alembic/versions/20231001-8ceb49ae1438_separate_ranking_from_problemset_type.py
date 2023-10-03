"""separate ranking from problemset type

Revision ID: 8ceb49ae1438
Revises: 
Create Date: 2023-10-01 17:27:40.375942

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '8ceb49ae1438'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('version')
    op.add_column('Contest', sa.Column('Ranked', sa.Boolean(), nullable=False))
    op.add_column('Contest', sa.Column('Rank_Penalty', sa.Boolean(), nullable=False))
    op.add_column('Contest', sa.Column('Rank_Partial_Score', sa.Boolean(), nullable=False))
    # ### end Alembic commands ###
    op.execute('UPDATE Contest SET Ranked = true WHERE Type IN (0, 2);')
    op.execute('UPDATE Contest SET Rank_Partial_Score = true WHERE Type IN (0, 2);')
    op.execute('UPDATE Contest SET Type = 1 WHERE Type = 0 AND INSTR(Name, \'作业\') > 0;')
    op.execute('UPDATE Contest SET Type = 1 WHERE Type = 0 AND INSTR(Name, \'测试\') > 0;')
    op.execute('UPDATE Contest SET Type = 1 WHERE Type = 0 AND INSTR(Name, \'练习\') > 0;')
    op.execute('UPDATE Contest SET Type = 1 WHERE Type = 0 AND INSTR(Name, \'Homework\') > 0;')
    op.execute('UPDATE Contest SET Type = 1 WHERE Type = 0 AND INSTR(Name, \'AI2615\') > 0;')


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('Contest', 'Rank_Partial_Score')
    op.drop_column('Contest', 'Rank_Penalty')
    op.drop_column('Contest', 'Ranked')
    Version = op.create_table('version',
    sa.Column('version', mysql.INTEGER(display_width=11), autoincrement=True, nullable=False),
    sa.PrimaryKeyConstraint('version'),
    mysql_collate='utf8mb4_general_ci',
    mysql_default_charset='utf8mb4',
    mysql_engine='InnoDB'
    )
    # ### end Alembic commands ###
    op.bulk_insert(Version, [{'version': 1}])