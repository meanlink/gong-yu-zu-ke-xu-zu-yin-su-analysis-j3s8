-- 分析批次：每批成功分析留档样本数据、计算结果与跟进预览，复核按批次快照派单
CREATE TABLE IF NOT EXISTS analysis_batch (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_by integer REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by integer REFERENCES users(id),
    reviewed_at timestamptz,
    review_note text,
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    sample_snapshot jsonb NOT NULL,
    analysis_result jsonb NOT NULL,
    followup_preview jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_batch_created_idx ON analysis_batch(created_at DESC);

-- 跟进任务关联到产生它的批次；同一批次内同一问卷至多派一条，保证重试不重复派单
ALTER TABLE follow_up_tasks ADD COLUMN IF NOT EXISTS batch_id integer REFERENCES analysis_batch(id);
CREATE UNIQUE INDEX IF NOT EXISTS follow_up_batch_survey_uniq
    ON follow_up_tasks(batch_id, survey_id) WHERE batch_id IS NOT NULL;
