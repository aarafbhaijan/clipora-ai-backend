"""Initial schema: 13 tables (architecture.md section 6).

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UP = [
    """CREATE EXTENSION IF NOT EXISTS vector""",
    """CREATE TABLE users (
	id UUID NOT NULL, 
	cognito_sub VARCHAR(128) NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	role VARCHAR(16) DEFAULT 'editor' NOT NULL, 
	learn_enabled BOOLEAN DEFAULT true NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (cognito_sub)
)""",
    """CREATE TABLE agent_memories (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	text TEXT NOT NULL, 
	embedding VECTOR(384), 
	evidence_count INTEGER DEFAULT '0' NOT NULL, 
	status VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	source_event_ids JSONB DEFAULT '[]' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)""",
    """CREATE TABLE app_settings (
	key VARCHAR(64) NOT NULL, 
	value JSONB NOT NULL, 
	updated_by UUID, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (key), 
	FOREIGN KEY(updated_by) REFERENCES users (id)
)""",
    """CREATE TABLE llm_usage (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	project_id UUID, 
	job_id UUID, 
	job_kind VARCHAR(16) NOT NULL, 
	node VARCHAR(64) NOT NULL, 
	model VARCHAR(128) NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cache_read_tokens INTEGER DEFAULT '0' NOT NULL, 
	cache_write_tokens INTEGER DEFAULT '0' NOT NULL, 
	cost_usd NUMERIC(12, 6), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)""",
    """CREATE INDEX ix_llm_usage_user_created ON llm_usage (user_id, created_at)""",
    """CREATE TABLE projects (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	source_key VARCHAR(512) NOT NULL, 
	upload_id VARCHAR(1024), 
	size_bytes BIGINT NOT NULL, 
	duration_s FLOAT, 
	width INTEGER, 
	height INTEGER, 
	fps FLOAT, 
	current_version INTEGER DEFAULT '0' NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)""",
    """CREATE INDEX ix_projects_user_created ON projects (user_id, created_at desc)""",
    """CREATE TABLE user_limits (
	user_id UUID NOT NULL, 
	key VARCHAR(64) NOT NULL, 
	value JSONB NOT NULL, 
	updated_by UUID, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (user_id, key), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(updated_by) REFERENCES users (id)
)""",
    """CREATE TABLE edl_versions (
	id UUID NOT NULL, 
	project_id UUID NOT NULL, 
	version INTEGER NOT NULL, 
	edl JSONB NOT NULL, 
	author VARCHAR(16) NOT NULL, 
	parent_version INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (project_id, version), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
)""",
    """CREATE TABLE feedback_events (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	project_id UUID, 
	target_kind VARCHAR(32) NOT NULL, 
	target_ref VARCHAR(128) NOT NULL, 
	signal VARCHAR(32) NOT NULL, 
	reason TEXT, 
	diff JSONB, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
)""",
    """CREATE TABLE jobs (
	id UUID NOT NULL, 
	project_id UUID, 
	user_id UUID NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	queue VARCHAR(16) NOT NULL, 
	payload JSONB DEFAULT '{}' NOT NULL, 
	status VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	stage VARCHAR(32), 
	percent INTEGER DEFAULT '0' NOT NULL, 
	stage_markers JSONB DEFAULT '{}' NOT NULL, 
	error_code VARCHAR(32), 
	error_detail TEXT, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	timeout_s INTEGER NOT NULL, 
	run_after TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	locked_by VARCHAR(128), 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)""",
    """CREATE INDEX ix_jobs_project_created ON jobs (project_id, created_at desc)""",
    """CREATE INDEX ix_jobs_queued ON jobs (queue, run_after) WHERE status = 'queued'""",
    """CREATE INDEX ix_jobs_running_lease ON jobs (queue, lease_expires_at) WHERE status = 'running'""",
    """CREATE UNIQUE INDEX ux_jobs_active_project_kind ON jobs (project_id, kind) WHERE project_id IS NOT NULL AND status IN ('queued','running','waiting_input')""",
    """CREATE UNIQUE INDEX ux_jobs_active_user_kind ON jobs (user_id, kind) WHERE project_id IS NULL AND status IN ('queued','running','waiting_input')""",
    """CREATE TABLE messages (
	id UUID NOT NULL, 
	project_id UUID NOT NULL, 
	thread VARCHAR(16) NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	content TEXT DEFAULT '' NOT NULL, 
	payload JSONB DEFAULT '{}' NOT NULL, 
	edl_version INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
)""",
    """CREATE INDEX ix_messages_project_thread ON messages (project_id, thread, created_at)""",
    """CREATE TABLE transcript_chunks (
	id UUID NOT NULL, 
	project_id UUID NOT NULL, 
	cue_from VARCHAR(16) NOT NULL, 
	cue_to VARCHAR(16) NOT NULL, 
	start_s FLOAT NOT NULL, 
	end_s FLOAT NOT NULL, 
	text TEXT NOT NULL, 
	embedding VECTOR(384) NOT NULL, 
	tsv TSVECTOR NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
)""",
    """CREATE INDEX ix_chunks_project ON transcript_chunks (project_id)""",
    """CREATE INDEX ix_chunks_tsv ON transcript_chunks USING gin (tsv)""",
    """CREATE TABLE exports (
	id UUID NOT NULL, 
	project_id UUID NOT NULL, 
	edl_version INTEGER NOT NULL, 
	job_id UUID, 
	status VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	output_key VARCHAR(512), 
	srt_key VARCHAR(512), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
)""",
    """CREATE TABLE metadata_sets (
	id UUID NOT NULL, 
	project_id UUID NOT NULL, 
	version INTEGER NOT NULL, 
	edl_version INTEGER NOT NULL, 
	export_id UUID, 
	platform VARCHAR(32), 
	language VARCHAR(16), 
	tone VARCHAR(16), 
	fields JSONB NOT NULL, 
	author VARCHAR(16) NOT NULL, 
	parent_version INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (project_id, version), 
	FOREIGN KEY(project_id) REFERENCES projects (id), 
	FOREIGN KEY(export_id) REFERENCES exports (id)
)""",
]

DOWN = [
    "DROP TABLE IF EXISTS metadata_sets",
    "DROP TABLE IF EXISTS exports",
    "DROP TABLE IF EXISTS transcript_chunks",
    "DROP TABLE IF EXISTS messages",
    "DROP TABLE IF EXISTS jobs",
    "DROP TABLE IF EXISTS feedback_events",
    "DROP TABLE IF EXISTS edl_versions",
    "DROP TABLE IF EXISTS user_limits",
    "DROP TABLE IF EXISTS projects",
    "DROP TABLE IF EXISTS llm_usage",
    "DROP TABLE IF EXISTS app_settings",
    "DROP TABLE IF EXISTS agent_memories",
    "DROP TABLE IF EXISTS users",
]


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)
