"""initial schema

Revision ID: a3a98be4c2c7
Revises: 
Create Date: 2026-09-14 23:39:01.465007

"""
from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a3a98be4c2c7'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions must exist before any vector/trgm column or index is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('embedding_cache',
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('content_hash', 'model')
    )
    op.create_table('eval_cases',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('repo_slug', sa.String(length=255), nullable=False),
    sa.Column('commit_sha', sa.String(length=40), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('expected_paths', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('expected_symbols', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('rubric', sa.Text(), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('eval_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('git_sha', sa.String(length=40), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=64), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('repositories',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('github_id', sa.BigInteger(), nullable=False),
    sa.Column('owner', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('default_branch', sa.String(length=255), nullable=False),
    sa.Column('is_private', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('github_id'),
    sa.UniqueConstraint('owner', 'name', name='uq_repositories_owner_name')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('github_id', sa.BigInteger(), nullable=False),
    sa.Column('login', sa.String(length=255), nullable=False),
    sa.Column('avatar_url', sa.Text(), nullable=True),
    sa.Column('access_token', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('github_id')
    )
    op.create_table('conversations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('repository_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversations_user_repo', 'conversations', ['user_id', 'repository_id'], unique=False)
    op.create_table('eval_results',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('eval_run_id', sa.UUID(), nullable=False),
    sa.Column('eval_case_id', sa.String(length=128), nullable=False),
    sa.Column('answer', sa.Text(), nullable=True),
    sa.Column('score', sa.Integer(), nullable=True),
    sa.Column('judge_reasoning', sa.Text(), nullable=True),
    sa.Column('file_recall', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('citation_validity', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('abstained', sa.Boolean(), nullable=True),
    sa.Column('tool_calls', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['eval_case_id'], ['eval_cases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['eval_run_id'], ['eval_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_eval_results_run', 'eval_results', ['eval_run_id'], unique=False)
    op.create_table('index_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('repository_id', sa.UUID(), nullable=False),
    sa.Column('commit_sha', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('phase_pct', sa.Integer(), nullable=False),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('overview', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repository_id', 'commit_sha', name='uq_index_runs_repo_commit')
    )
    op.create_index('ix_index_runs_repo_status', 'index_runs', ['repository_id', 'status'], unique=False)
    op.create_table('repo_access',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('repository_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'repository_id')
    )
    op.create_table('files',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('index_run_id', sa.UUID(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=32), nullable=True),
    sa.Column('loc', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['index_run_id'], ['index_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('index_run_id', 'path', name='uq_files_run_path')
    )
    op.create_index('ix_files_content_hash', 'files', ['content_hash'], unique=False)
    op.create_index('ix_files_run_lang', 'files', ['index_run_id', 'language'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('index_run_id', sa.UUID(), nullable=True),
    sa.Column('confidence', sa.String(length=16), nullable=True),
    sa.Column('prompt_version', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('total_cost_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('truncated', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['index_run_id'], ['index_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_messages_conversation', 'messages', ['conversation_id', 'created_at'], unique=False)
    op.create_table('agent_steps',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('idx', sa.Integer(), nullable=False),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('tool_input', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('result_summary', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('tokens_in', sa.Integer(), nullable=True),
    sa.Column('tokens_out', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_steps_message', 'agent_steps', ['message_id', 'idx'], unique=False)
    op.create_table('citations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_citations_message', 'citations', ['message_id'], unique=False)
    op.create_table('symbols',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('index_run_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=512), nullable=False),
    sa.Column('qualified_name', sa.Text(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=True),
    sa.Column('docstring', sa.Text(), nullable=True),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.CheckConstraint('end_line >= start_line', name='ck_symbols_line_order'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['index_run_id'], ['index_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_id'], ['symbols.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_symbols_file', 'symbols', ['file_id'], unique=False)
    op.create_index('ix_symbols_name_trgm', 'symbols', ['name'], unique=False, postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('ix_symbols_run_qname', 'symbols', ['index_run_id', 'qualified_name'], unique=False)
    op.create_table('chunks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('index_run_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=False),
    sa.Column('symbol_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('start_line', sa.Integer(), nullable=False),
    sa.Column('end_line', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.CheckConstraint('end_line >= start_line', name='ck_chunks_line_order'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['index_run_id'], ['index_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['symbol_id'], ['symbols.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chunks_content_hash', 'chunks', ['content_hash'], unique=False)
    op.create_index('ix_chunks_embedding_hnsw', 'chunks', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'}, postgresql_with={'m': 16, 'ef_construction': 64})
    op.create_index('ix_chunks_file', 'chunks', ['file_id'], unique=False)
    op.create_index('ix_chunks_run_kind', 'chunks', ['index_run_id', 'kind'], unique=False)
    op.create_table('symbol_edges',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('index_run_id', sa.UUID(), nullable=False),
    sa.Column('src_symbol_id', sa.UUID(), nullable=False),
    sa.Column('dst_symbol_id', sa.UUID(), nullable=True),
    sa.Column('dst_name', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('line', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['dst_symbol_id'], ['symbols.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['index_run_id'], ['index_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['src_symbol_id'], ['symbols.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_edges_dst', 'symbol_edges', ['dst_symbol_id', 'kind'], unique=False)
    op.create_index('ix_edges_run_dstname', 'symbol_edges', ['index_run_id', 'dst_name'], unique=False)
    op.create_index('ix_edges_src', 'symbol_edges', ['src_symbol_id', 'kind'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_edges_src', table_name='symbol_edges')
    op.drop_index('ix_edges_run_dstname', table_name='symbol_edges')
    op.drop_index('ix_edges_dst', table_name='symbol_edges')
    op.drop_table('symbol_edges')
    op.drop_index('ix_chunks_run_kind', table_name='chunks')
    op.drop_index('ix_chunks_file', table_name='chunks')
    op.drop_index('ix_chunks_embedding_hnsw', table_name='chunks', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'}, postgresql_with={'m': 16, 'ef_construction': 64})
    op.drop_index('ix_chunks_content_hash', table_name='chunks')
    op.drop_table('chunks')
    op.drop_index('ix_symbols_run_qname', table_name='symbols')
    op.drop_index('ix_symbols_name_trgm', table_name='symbols', postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.drop_index('ix_symbols_file', table_name='symbols')
    op.drop_table('symbols')
    op.drop_index('ix_citations_message', table_name='citations')
    op.drop_table('citations')
    op.drop_index('ix_agent_steps_message', table_name='agent_steps')
    op.drop_table('agent_steps')
    op.drop_index('ix_messages_conversation', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_files_run_lang', table_name='files')
    op.drop_index('ix_files_content_hash', table_name='files')
    op.drop_table('files')
    op.drop_table('repo_access')
    op.drop_index('ix_index_runs_repo_status', table_name='index_runs')
    op.drop_table('index_runs')
    op.drop_index('ix_eval_results_run', table_name='eval_results')
    op.drop_table('eval_results')
    op.drop_index('ix_conversations_user_repo', table_name='conversations')
    op.drop_table('conversations')
    op.drop_table('users')
    op.drop_table('repositories')
    op.drop_table('eval_runs')
    op.drop_table('eval_cases')
    op.drop_table('embedding_cache')
    # ### end Alembic commands ###
