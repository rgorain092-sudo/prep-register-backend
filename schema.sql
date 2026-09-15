-- ============================================================
-- AI Learning Platform - Core Schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TYPE learning_domain AS ENUM ('EXAMS', 'SKILLS', 'LANGUAGES');
CREATE TYPE material_type AS ENUM ('SYLLABUS', 'NOTES', 'CURRENT_AFFAIRS', 'NCERT', 'MOCK_TEST', 'EXAM_PATTERN');
CREATE TYPE upload_status AS ENUM ('UPLOADING', 'PROCESSING', 'READY', 'FAILED');
CREATE TYPE difficulty_level AS ENUM ('EASY', 'MEDIUM', 'HARD', 'EXPERT');

-- 1. Users (with real auth fields)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(100) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    selected_exams VARCHAR(50)[] DEFAULT '{}',
    active_skills VARCHAR(50)[] DEFAULT '{}',
    active_languages VARCHAR(50)[] DEFAULT '{}',
    daily_study_hours INT DEFAULT 4,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 2. Knowledge base (every uploaded file, any size)
CREATE TABLE knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_name VARCHAR(255) NOT NULL,
    cloud_storage_key TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    domain learning_domain,
    target_subject VARCHAR(100),
    content_category material_type,
    ai_tags JSONB,
    status upload_status NOT NULL DEFAULT 'UPLOADING',
    failure_reason TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- 3. Multipart upload sessions (so client can resume/verify)
CREATE TABLE upload_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID REFERENCES knowledge_base(id) ON DELETE CASCADE,
    s3_upload_id TEXT NOT NULL,
    file_key TEXT NOT NULL,
    total_parts INT,
    completed_parts INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 4. AI-generated study routines
CREATE TABLE study_routines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day_of_week INT CHECK (day_of_week BETWEEN 1 AND 7),
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    focus_domain learning_domain NOT NULL,
    topic_headline VARCHAR(255) NOT NULL,
    linked_material_id UUID REFERENCES knowledge_base(id) ON DELETE SET NULL,
    completed BOOLEAN DEFAULT FALSE
);

-- 5. Mock tests
CREATE TABLE mock_test_series (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(255) NOT NULL,
    associated_domain learning_domain NOT NULL,
    target_tag VARCHAR(100) NOT NULL,
    difficulty difficulty_level NOT NULL DEFAULT 'MEDIUM',
    duration_minutes INT NOT NULL,
    total_marks INT NOT NULL,
    question_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 6. Attempts / analytics
CREATE TABLE test_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    test_id UUID NOT NULL REFERENCES mock_test_series(id) ON DELETE CASCADE,
    score_obtained NUMERIC(6,2) NOT NULL,
    accuracy_percentage NUMERIC(5,2) NOT NULL,
    ai_performance_feedback TEXT,
    attempted_at TIMESTAMPTZ DEFAULT now()
);

-- 7. Current affairs feed (daily digest, per exam)
CREATE TABLE current_affairs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_exam VARCHAR(50) NOT NULL,
    headline VARCHAR(500) NOT NULL,
    summary TEXT NOT NULL,
    source_url TEXT,
    relevance_tags VARCHAR(50)[] DEFAULT '{}',
    published_date DATE NOT NULL DEFAULT CURRENT_DATE
);

-- Indexes
CREATE INDEX idx_kb_user_domain ON knowledge_base(user_id, domain);
CREATE INDEX idx_kb_status ON knowledge_base(status);
CREATE INDEX idx_routine_lookup ON study_routines(user_id, day_of_week);
CREATE INDEX idx_attempts_user ON test_attempts(user_id, attempted_at DESC);
CREATE INDEX idx_mock_tests_domain_difficulty ON mock_test_series(associated_domain, target_tag, difficulty);
CREATE INDEX idx_current_affairs_exam_date ON current_affairs(target_exam, published_date DESC);
