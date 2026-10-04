"""openshift

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04 17:20:54.078493
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import openbackup.db.models

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('kube_clusters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('api_url', sa.String(length=512), nullable=False),
    sa.Column('ca_pem', sa.Text(), nullable=False),
    sa.Column('backup_token_enc', sa.Text(), nullable=False),
    sa.Column('restore_token_enc', sa.Text(), nullable=True),
    sa.Column('vcenter_id', sa.Integer(), nullable=True),
    sa.Column('created_at', openbackup.db.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['vcenter_id'], ['vcenters.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.Enum('vsphere', 'openshift', 'etcd', name='jobkind', native_enum=False), server_default='vsphere', nullable=False))
        batch_op.add_column(sa.Column('cluster_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('selection', sa.JSON(), server_default='{}', nullable=False))
        batch_op.alter_column('vcenter_id',
               existing_type=sa.INTEGER(),
               nullable=True)
        batch_op.create_foreign_key('fk_jobs_cluster_id', 'kube_clusters', ['cluster_id'], ['id'])

    with op.batch_alter_table('restore_points', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subject_kind', sa.String(length=16), server_default='vm', nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('restore_points', schema=None) as batch_op:
        batch_op.drop_column('subject_kind')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_jobs_cluster_id', type_='foreignkey')
        batch_op.alter_column('vcenter_id',
               existing_type=sa.INTEGER(),
               nullable=False)
        batch_op.drop_column('selection')
        batch_op.drop_column('cluster_id')
        batch_op.drop_column('kind')

    op.drop_table('kube_clusters')
