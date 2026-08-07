"""multi-agent research tables

Revision ID: 0007_multiagent_research
Revises: 0006_daily_regimes

Adds the `ma_*` schema for the multi-agent options research pipeline.

**Strictly additive.** Every statement is a CREATE; no existing table, column or
index is altered or dropped. That is deliberate rather than incidental: the
short-duration scoring model is frozen for the capture window (CLAUDE.md 2), and
an ALTER on a table that model reads would be a behaviour change smuggled in as a
schema change — the shape of failure FINDING_01 documented.

Autogenerate additionally proposed a large set of NOT NULL alterations on
pre-existing tables. Those come from SQLAlchemy's server-default handling under
SQLite, not from any intent of this change, and they were removed by hand. If
those columns genuinely need tightening it belongs in its own migration with its
own review, not as a side effect of adding a subsystem.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0007_multiagent_research"
down_revision: str | None = "0006_daily_regimes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ma_option_contract_snapshots',
    sa.Column('structure_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('strategy_type', sa.String(length=32), nullable=False),
    sa.Column('selected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expiration', sa.Date(), nullable=True),
    sa.Column('underlying_price', sa.Float(), nullable=True),
    sa.Column('net_debit_per_share', sa.Float(), nullable=True),
    sa.Column('contracts', sa.Integer(), nullable=False),
    sa.Column('max_loss', sa.Float(), nullable=True),
    sa.Column('max_profit', sa.Float(), nullable=True),
    sa.Column('breakeven', sa.Float(), nullable=True),
    sa.Column('reward_to_risk', sa.Float(), nullable=True),
    sa.Column('worst_leg_spread_pct', sa.Float(), nullable=True),
    sa.Column('min_open_interest', sa.Integer(), nullable=True),
    sa.Column('min_volume', sa.Integer(), nullable=True),
    sa.Column('net_delta', sa.Float(), nullable=True),
    sa.Column('net_theta', sa.Float(), nullable=True),
    sa.Column('net_vega', sa.Float(), nullable=True),
    sa.Column('greeks_source', sa.String(length=16), nullable=False),
    sa.Column('probability_of_profit', sa.Float(), nullable=True),
    sa.Column('cost_drag_pct', sa.Float(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('structure_id')
    )
    op.create_index(op.f('ix_ma_option_contract_snapshots_candidate_id'), 'ma_option_contract_snapshots', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_option_contract_snapshots_expiration'), 'ma_option_contract_snapshots', ['expiration'], unique=False)
    op.create_index(op.f('ix_ma_option_contract_snapshots_run_id'), 'ma_option_contract_snapshots', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_option_contract_snapshots_strategy_type'), 'ma_option_contract_snapshots', ['strategy_type'], unique=False)
    op.create_index(op.f('ix_ma_option_contract_snapshots_ticker'), 'ma_option_contract_snapshots', ['ticker'], unique=False)
    op.create_table('ma_options_flow_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('alerts_considered', sa.Integer(), nullable=False),
    sa.Column('call_premium', sa.Float(), nullable=True),
    sa.Column('put_premium', sa.Float(), nullable=True),
    sa.Column('net_premium', sa.Float(), nullable=True),
    sa.Column('ask_side_premium', sa.Float(), nullable=True),
    sa.Column('sweep_count', sa.Integer(), nullable=False),
    sa.Column('implied_bias', sa.String(length=16), nullable=False),
    sa.Column('direction_ambiguous', sa.Boolean(), nullable=False),
    sa.Column('verdict', sa.String(length=24), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_options_flow_snapshots_candidate_id'), 'ma_options_flow_snapshots', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_options_flow_snapshots_run_id'), 'ma_options_flow_snapshots', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_options_flow_snapshots_symbol'), 'ma_options_flow_snapshots', ['symbol'], unique=False)
    op.create_index(op.f('ix_ma_options_flow_snapshots_verdict'), 'ma_options_flow_snapshots', ['verdict'], unique=False)
    op.create_table('ma_runs',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stage', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('methodology_version', sa.String(length=64), nullable=False),
    sa.Column('scoring_model_version', sa.String(length=64), nullable=False),
    sa.Column('agent_runner', sa.String(length=64), nullable=False),
    sa.Column('trading_mode', sa.String(length=24), nullable=False),
    sa.Column('execution_enabled', sa.Boolean(), nullable=False),
    sa.Column('contracts_finalised', sa.Boolean(), nullable=False),
    sa.Column('stage_note', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_index(op.f('ix_ma_runs_agent_runner'), 'ma_runs', ['agent_runner'], unique=False)
    op.create_index(op.f('ix_ma_runs_started_at'), 'ma_runs', ['started_at'], unique=False)
    op.create_index(op.f('ix_ma_runs_status'), 'ma_runs', ['status'], unique=False)
    op.create_table('ma_score_components',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=40), nullable=False),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('points_awarded', sa.Float(), nullable=False),
    sa.Column('points_available', sa.Float(), nullable=False),
    sa.Column('normalized', sa.Float(), nullable=True),
    sa.Column('abstained', sa.Boolean(), nullable=False),
    sa.Column('coverage', sa.Float(), nullable=True),
    sa.Column('rules', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ma_score_comp_cat_run', 'ma_score_components', ['category', 'run_id'], unique=False)
    op.create_index(op.f('ix_ma_score_components_abstained'), 'ma_score_components', ['abstained'], unique=False)
    op.create_index(op.f('ix_ma_score_components_candidate_id'), 'ma_score_components', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_score_components_category'), 'ma_score_components', ['category'], unique=False)
    op.create_index(op.f('ix_ma_score_components_run_id'), 'ma_score_components', ['run_id'], unique=False)
    op.create_table('ma_technical_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('price', sa.Float(), nullable=True),
    sa.Column('trend_bias', sa.String(length=16), nullable=False),
    sa.Column('bars_available', sa.Integer(), nullable=False),
    sa.Column('measurements', sa.JSON(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_technical_snapshots_candidate_id'), 'ma_technical_snapshots', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_technical_snapshots_run_id'), 'ma_technical_snapshots', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_technical_snapshots_symbol'), 'ma_technical_snapshots', ['symbol'], unique=False)
    op.create_table('ma_trade_decisions',
    sa.Column('decision_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('decision_id')
    )
    op.create_index(op.f('ix_ma_trade_decisions_action'), 'ma_trade_decisions', ['action'], unique=False)
    op.create_index(op.f('ix_ma_trade_decisions_candidate_id'), 'ma_trade_decisions', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_decisions_decided_at'), 'ma_trade_decisions', ['decided_at'], unique=False)
    op.create_index(op.f('ix_ma_trade_decisions_run_id'), 'ma_trade_decisions', ['run_id'], unique=False)
    op.create_table('ma_agent_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('agent', sa.String(length=40), nullable=False),
    sa.Column('stage', sa.String(length=24), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('runner', sa.String(length=64), nullable=False),
    sa.Column('definition_path', sa.Text(), nullable=True),
    sa.Column('prompt_excerpt', sa.Text(), nullable=False),
    sa.Column('raw_response_excerpt', sa.Text(), nullable=False),
    sa.Column('structured_output', sa.JSON(), nullable=True),
    sa.Column('tools_used', sa.JSON(), nullable=False),
    sa.Column('providers_queried', sa.JSON(), nullable=False),
    sa.Column('errors', sa.JSON(), nullable=False),
    sa.Column('missing_data', sa.JSON(), nullable=False),
    sa.Column('validation_warnings', sa.JSON(), nullable=False),
    sa.Column('dropped_claims', sa.JSON(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_agent_runs_agent'), 'ma_agent_runs', ['agent'], unique=False)
    op.create_index(op.f('ix_ma_agent_runs_run_id'), 'ma_agent_runs', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_agent_runs_runner'), 'ma_agent_runs', ['runner'], unique=False)
    op.create_index(op.f('ix_ma_agent_runs_status'), 'ma_agent_runs', ['status'], unique=False)
    op.create_table('ma_data_provider_requests',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('capability', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('result_count', sa.Integer(), nullable=True),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_data_provider_requests_capability'), 'ma_data_provider_requests', ['capability'], unique=False)
    op.create_index(op.f('ix_ma_data_provider_requests_ok'), 'ma_data_provider_requests', ['ok'], unique=False)
    op.create_index(op.f('ix_ma_data_provider_requests_provider'), 'ma_data_provider_requests', ['provider'], unique=False)
    op.create_index(op.f('ix_ma_data_provider_requests_run_id'), 'ma_data_provider_requests', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_data_provider_requests_symbol'), 'ma_data_provider_requests', ['symbol'], unique=False)
    op.create_table('ma_data_quality_flags',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('flag', sa.String(length=40), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_data_quality_flags_flag'), 'ma_data_quality_flags', ['flag'], unique=False)
    op.create_index(op.f('ix_ma_data_quality_flags_run_id'), 'ma_data_quality_flags', ['run_id'], unique=False)
    op.create_table('ma_economic_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('evidence_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('catalyst_type', sa.String(length=40), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('importance', sa.String(length=16), nullable=False),
    sa.Column('consensus', sa.Float(), nullable=True),
    sa.Column('previous', sa.Float(), nullable=True),
    sa.Column('actual', sa.Float(), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_economic_events_catalyst_type'), 'ma_economic_events', ['catalyst_type'], unique=False)
    op.create_index(op.f('ix_ma_economic_events_evidence_id'), 'ma_economic_events', ['evidence_id'], unique=False)
    op.create_index(op.f('ix_ma_economic_events_run_id'), 'ma_economic_events', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_economic_events_scheduled_at'), 'ma_economic_events', ['scheduled_at'], unique=False)
    op.create_table('ma_market_briefs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('market_regime', sa.String(length=24), nullable=False),
    sa.Column('volatility_regime', sa.String(length=24), nullable=False),
    sa.Column('spy_bias', sa.String(length=16), nullable=False),
    sa.Column('qqq_bias', sa.String(length=16), nullable=False),
    sa.Column('vix_level', sa.Float(), nullable=True),
    sa.Column('relevance_confidence', sa.Float(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_market_briefs_market_regime'), 'ma_market_briefs', ['market_regime'], unique=False)
    op.create_index(op.f('ix_ma_market_briefs_run_id'), 'ma_market_briefs', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_market_briefs_volatility_regime'), 'ma_market_briefs', ['volatility_regime'], unique=False)
    op.create_table('ma_news_items',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('evidence_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=True),
    sa.Column('headline', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=96), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('catalyst_type', sa.String(length=40), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('evidence_quality', sa.String(length=24), nullable=False),
    sa.Column('relevance_confidence', sa.Float(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_news_items_catalyst_type'), 'ma_news_items', ['catalyst_type'], unique=False)
    op.create_index(op.f('ix_ma_news_items_evidence_id'), 'ma_news_items', ['evidence_id'], unique=False)
    op.create_index(op.f('ix_ma_news_items_published_at'), 'ma_news_items', ['published_at'], unique=False)
    op.create_index(op.f('ix_ma_news_items_run_id'), 'ma_news_items', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_news_items_symbol'), 'ma_news_items', ['symbol'], unique=False)
    op.create_table('ma_stock_catalysts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('catalyst_type', sa.String(length=40), nullable=False),
    sa.Column('headline', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=96), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expected_direction', sa.String(length=16), nullable=False),
    sa.Column('importance', sa.String(length=16), nullable=False),
    sa.Column('importance_score', sa.Float(), nullable=True),
    sa.Column('expected_time_horizon', sa.String(length=16), nullable=False),
    sa.Column('scheduled_event_date', sa.Date(), nullable=True),
    sa.Column('evidence_quality', sa.String(length=24), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('evidence_refs', sa.JSON(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ma_catalyst_ticker_type', 'ma_stock_catalysts', ['ticker', 'catalyst_type'], unique=False)
    op.create_index(op.f('ix_ma_stock_catalysts_catalyst_type'), 'ma_stock_catalysts', ['catalyst_type'], unique=False)
    op.create_index(op.f('ix_ma_stock_catalysts_evidence_quality'), 'ma_stock_catalysts', ['evidence_quality'], unique=False)
    op.create_index(op.f('ix_ma_stock_catalysts_expected_direction'), 'ma_stock_catalysts', ['expected_direction'], unique=False)
    op.create_index(op.f('ix_ma_stock_catalysts_run_id'), 'ma_stock_catalysts', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_stock_catalysts_ticker'), 'ma_stock_catalysts', ['ticker'], unique=False)
    op.create_table('ma_trade_candidates',
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('strategy_type', sa.String(length=32), nullable=False),
    sa.Column('thesis', sa.Text(), nullable=False),
    sa.Column('primary_catalyst', sa.Text(), nullable=False),
    sa.Column('expected_holding_period', sa.String(length=16), nullable=False),
    sa.Column('expected_move_pct', sa.Float(), nullable=True),
    sa.Column('underlying_reference_price', sa.Float(), nullable=True),
    sa.Column('invalidation_thesis', sa.Text(), nullable=False),
    sa.Column('earnings_date', sa.Date(), nullable=True),
    sa.Column('catalyst_date', sa.Date(), nullable=True),
    sa.Column('preliminary_quality', sa.String(length=16), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['ma_runs.run_id'], ),
    sa.PrimaryKeyConstraint('candidate_id')
    )
    op.create_index(op.f('ix_ma_trade_candidates_direction'), 'ma_trade_candidates', ['direction'], unique=False)
    op.create_index(op.f('ix_ma_trade_candidates_expected_holding_period'), 'ma_trade_candidates', ['expected_holding_period'], unique=False)
    op.create_index(op.f('ix_ma_trade_candidates_run_id'), 'ma_trade_candidates', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_candidates_strategy_type'), 'ma_trade_candidates', ['strategy_type'], unique=False)
    op.create_index(op.f('ix_ma_trade_candidates_ticker'), 'ma_trade_candidates', ['ticker'], unique=False)
    op.create_table('ma_trade_executions',
    sa.Column('execution_id', sa.String(length=64), nullable=False),
    sa.Column('decision_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('contract_description', sa.Text(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('entry_price_per_contract', sa.Float(), nullable=False),
    sa.Column('underlying_price_at_entry', sa.Float(), nullable=True),
    sa.Column('stop_or_invalidation', sa.Text(), nullable=False),
    sa.Column('target', sa.Text(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['ma_trade_decisions.decision_id'], ),
    sa.PrimaryKeyConstraint('execution_id')
    )
    op.create_index(op.f('ix_ma_trade_executions_candidate_id'), 'ma_trade_executions', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_executions_decision_id'), 'ma_trade_executions', ['decision_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_executions_entered_at'), 'ma_trade_executions', ['entered_at'], unique=False)
    op.create_table('ma_trade_recommendations',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('strategy_type', sa.String(length=32), nullable=False),
    sa.Column('scored_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('methodology_version', sa.String(length=64), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('raw_points', sa.Float(), nullable=False),
    sa.Column('measured_weight', sa.Float(), nullable=False),
    sa.Column('input_coverage', sa.Float(), nullable=False),
    sa.Column('classification', sa.String(length=24), nullable=False),
    sa.Column('calibration_status', sa.String(length=16), nullable=False),
    sa.Column('is_ranked', sa.Boolean(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=True),
    sa.Column('hard_rejected', sa.Boolean(), nullable=False),
    sa.Column('rejection_codes', sa.JSON(), nullable=False),
    sa.Column('rejection_reasons', sa.JSON(), nullable=False),
    sa.Column('structure_id', sa.String(length=64), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['ma_trade_candidates.candidate_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ma_reco_score_class', 'ma_trade_recommendations', ['score', 'classification'], unique=False)
    op.create_index('ix_ma_reco_ticker_run', 'ma_trade_recommendations', ['ticker', 'run_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_calibration_status'), 'ma_trade_recommendations', ['calibration_status'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_candidate_id'), 'ma_trade_recommendations', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_classification'), 'ma_trade_recommendations', ['classification'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_hard_rejected'), 'ma_trade_recommendations', ['hard_rejected'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_is_ranked'), 'ma_trade_recommendations', ['is_ranked'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_methodology_version'), 'ma_trade_recommendations', ['methodology_version'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_run_id'), 'ma_trade_recommendations', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_score'), 'ma_trade_recommendations', ['score'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_strategy_type'), 'ma_trade_recommendations', ['strategy_type'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_structure_id'), 'ma_trade_recommendations', ['structure_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_recommendations_ticker'), 'ma_trade_recommendations', ['ticker'], unique=False)
    op.create_table('ma_trade_results',
    sa.Column('result_id', sa.String(length=64), nullable=False),
    sa.Column('execution_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('exited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_price_per_contract', sa.Float(), nullable=True),
    sa.Column('realized_pnl', sa.Float(), nullable=True),
    sa.Column('max_favorable_excursion_bound', sa.Float(), nullable=True),
    sa.Column('max_adverse_excursion_bound', sa.Float(), nullable=True),
    sa.Column('underlying_price_at_exit', sa.Float(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['ma_trade_executions.execution_id'], ),
    sa.PrimaryKeyConstraint('result_id')
    )
    op.create_index(op.f('ix_ma_trade_results_candidate_id'), 'ma_trade_results', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_results_execution_id'), 'ma_trade_results', ['execution_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_results_exited_at'), 'ma_trade_results', ['exited_at'], unique=False)
    op.create_index(op.f('ix_ma_trade_results_realized_pnl'), 'ma_trade_results', ['realized_pnl'], unique=False)
    op.create_table('ma_trade_validations',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('stage', sa.String(length=24), nullable=False),
    sa.Column('overall_verdict', sa.String(length=24), nullable=False),
    sa.Column('catalyst_verdict', sa.String(length=24), nullable=True),
    sa.Column('flow_verdict', sa.String(length=24), nullable=True),
    sa.Column('selected_structure_id', sa.String(length=64), nullable=True),
    sa.Column('agent_commentary', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['ma_trade_candidates.candidate_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ma_trade_validations_candidate_id'), 'ma_trade_validations', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_validations_overall_verdict'), 'ma_trade_validations', ['overall_verdict'], unique=False)
    op.create_index(op.f('ix_ma_trade_validations_run_id'), 'ma_trade_validations', ['run_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_validations_selected_structure_id'), 'ma_trade_validations', ['selected_structure_id'], unique=False)
    op.create_index(op.f('ix_ma_trade_validations_ticker'), 'ma_trade_validations', ['ticker'], unique=False)


def downgrade() -> None:
    """Drop the ma_* tables in reverse creation order.

    Indexes go with their tables, so they are not dropped individually.
    """
    op.drop_table('ma_trade_validations')
    op.drop_table('ma_trade_results')
    op.drop_table('ma_trade_recommendations')
    op.drop_table('ma_trade_executions')
    op.drop_table('ma_trade_candidates')
    op.drop_table('ma_stock_catalysts')
    op.drop_table('ma_news_items')
    op.drop_table('ma_market_briefs')
    op.drop_table('ma_economic_events')
    op.drop_table('ma_data_quality_flags')
    op.drop_table('ma_data_provider_requests')
    op.drop_table('ma_agent_runs')
    op.drop_table('ma_trade_decisions')
    op.drop_table('ma_technical_snapshots')
    op.drop_table('ma_score_components')
    op.drop_table('ma_runs')
    op.drop_table('ma_options_flow_snapshots')
    op.drop_table('ma_option_contract_snapshots')
