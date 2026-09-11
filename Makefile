.PHONY: help install test test-unit test-integration test-all test-parallel test-interfaces test-plugins test-performance test-resilience lint format clean coverage coverage-baseline coverage-artifact-guard benchmark security docker release-smoke release-smoke-container
.PHONY: alpha-dependency-sync alpha-format-lint alpha-mypy-ratchet alpha-unit-release-smoke alpha-integration-smoke alpha-docs-truth alpha-production-matrix alpha-release-gates
.PHONY: agent-doctor agent-fast agent-gate agent-full agent-fix agent-affected
.PHONY: agent-fast-local agent-gate-local agent-full-local agent-fix-local
.PHONY: docker-up docker-down docker-dev docker-prod docker-test docker-logs docker-health
.PHONY: test-dormant test-real-world test-semantic test-redis test-advanced test-cross-lang
.PHONY: setup-env setup-dev-env setup-prod-env backup restore clean-docker check-diagrams
.PHONY: semantic-up semantic-down semantic-preflight semantic-setup

UV_RUN := UV_LINK_MODE=copy uv run
COVERAGE_PYTEST_ADDOPTS := --override-ini=addopts="--verbose --strict-markers --tb=short --benchmark-disable -p no:warnings -m 'not integration and not slow'"
COVERAGE_BASELINE_TESTS := \
	tests/test_coverage_artifact_guard.py \
	tests/test_coverage_command_contract.py \
	tests/test_coverage_workflow_posture.py \
	tests/test_release_metadata.py \
	tests/smoke/test_release_smoke_contract.py \
	tests/docs/test_p23_doc_truth.py \
	tests/test_agent_validation.py \
	tests/test_localci_workflow_posture.py \
	tests/docs/test_coverage_docs.py \
	tests/docs/test_localci_agent_validation_docs.py \
	tests/docs/test_localci_validation_contract.py \
	tests/test_multi_repo_production_matrix.py \
	tests/test_multi_repo_failure_matrix.py \
	tests/test_repository_readiness.py \
	tests/test_tool_readiness_fail_closed.py \
	tests/test_git_index_manager.py \
	tests/test_ref_poller_edges.py \
	tests/test_rename_atomicity.py \
	tests/test_artifact_download.py
COVERAGE_TESTS := $(COVERAGE_BASELINE_TESTS) tests/docs/test_coverage_evidence_contract.py

help:
	@echo "Available commands:"
	@echo ""
	@echo "🐍 Python Development:"
	@echo "  install          Install dependencies"
	@echo "  test            Run unit tests"
	@echo "  test-unit       Run unit tests only"
	@echo "  test-integration Run integration tests only"
	@echo "  test-all        Run all tests with coverage"
	@echo "  test-parallel   Run comprehensive parallel test suite"
	@echo "  test-interfaces Test interface compliance"
	@echo "  test-plugins    Test all plugin functionality"
	@echo "  test-performance Test performance and SLOs"
	@echo "  test-resilience Test error handling and edge cases"
	@echo "  lint            Run linters"
	@echo "  format          Format code"
	@echo "  coverage        Generate coverage report"
	@echo "  benchmark       Run performance benchmarks"
	@echo "  security        Run security checks"
	@echo "  release-smoke   Run wheel and lexical MCP release smoke"
	@echo "  release-smoke-container Run production container release smoke"
	@echo "  alpha-production-matrix Run P33 production multi-repo release gate"
	@echo "  agent-doctor    Report local/offload validation readiness"
	@echo "  agent-fast      Run the cheap offline-first validation gate"
	@echo "  agent-gate      Run the pre-PR validation gate"
	@echo "  agent-full      Run the full validation gate including container smoke"
	@echo "  agent-fix       Run deterministic local formatting fixes"
	@echo "  agent-affected  Route changed files to agent-fast or agent-gate"
	@echo ""
	@echo "🐳 Docker Operations:"
	@echo "  docker          Build Docker image"
	@echo "  docker-up       Start all services"
	@echo "  docker-dev      Start development environment"
	@echo "  docker-prod     Start production environment"
	@echo "  docker-down     Stop all services"
	@echo "  docker-logs     View service logs"
	@echo "  docker-health   Check service health"
	@echo "  docker-test     Run tests in Docker"
	@echo ""
	@echo "🧪 Dormant Features Testing:"
	@echo "  test-dormant    Test all dormant features"
	@echo "  test-real-world Test with real repositories"
	@echo "  test-semantic   Test semantic search"
	@echo "  test-redis      Test Redis caching"
	@echo "  test-advanced   Test advanced indexing"
	@echo "  test-cross-lang Test cross-language features"
	@echo ""
	@echo "🔧 Environment Setup:"
	@echo "  setup-env       Setup basic environment files"
	@echo "  setup-dev-env   Setup development environment"
	@echo "  setup-prod-env  Setup production environment"
	@echo "  semantic-setup  Run semantic onboarding flow"
	@echo "  semantic-preflight Validate semantic stack readiness"
	@echo "  semantic-up     Start local Qdrant (docker-only)"
	@echo "  semantic-down   Stop local Qdrant"
	@echo ""
	@echo "💾 Data Management:"
	@echo "  backup          Backup all data"
	@echo "  restore         Restore from backup"
	@echo "  clean           Clean up temporary files"
	@echo "  clean-docker    Clean up Docker resources"

install:
	uv sync

test:
	pytest tests -v -m "not integration and not slow and not benchmark"

test-unit:
	pytest tests -v -m "unit"

test-integration:
	pytest tests -v -m "integration"

test-all:
	pytest tests -v --cov=mcp_server --cov-report=term-missing --cov-report=html

lint:
	black --check mcp_server tests
	isort --check-only mcp_server tests
	flake8 mcp_server tests
	mypy mcp_server
	pylint mcp_server

format:
	black mcp_server tests
	isort mcp_server tests


check-diagrams:
	python scripts/check_mermaid_diagrams.py architecture/portable_index_architecture.md architecture/index_artifact_architecture.md docs/implementation/PRODUCTIONIZATION_PLAN_2026-02.md

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage*" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name "htmlcov" -exec rm -rf {} +
	find . -type d -name ".tox" -exec rm -rf {} +
	find . -type f -name "coverage.xml" -delete
	find . -type f -name "coverage.json" -delete

coverage-artifact-guard:
	$(UV_RUN) --extra dev python scripts/check_coverage_artifacts.py

coverage-baseline: coverage-artifact-guard
	$(UV_RUN) --extra dev pytest $(COVERAGE_BASELINE_TESTS) $(COVERAGE_PYTEST_ADDOPTS) --cov=mcp_server --cov-report=term-missing --cov-report=html --cov-report=xml
	@echo "Coverage baseline generated in htmlcov/index.html"

coverage: coverage-artifact-guard
	$(UV_RUN) --extra dev pytest $(COVERAGE_TESTS) $(COVERAGE_PYTEST_ADDOPTS) --cov=mcp_server --cov-report=term-missing --cov-report=html --cov-report=xml
	@echo "Coverage report generated in htmlcov/index.html"

# Comprehensive parallel testing
test-parallel:
	@echo "🧪 Running comprehensive parallel test suite..."
	python run_parallel_tests.py

test-interfaces:
	@echo "🔧 Testing interface compliance..."
	python run_parallel_tests.py --phases interface_compliance

test-plugins:
	@echo "🔌 Testing plugin functionality..."
	python run_parallel_tests.py --phases plugin_functionality

test-performance:
	@echo "⚡ Testing performance and SLOs..."
	python run_parallel_tests.py --phases performance_validation

test-resilience:
	@echo "🛡️ Testing error handling and resilience..."
	python run_parallel_tests.py --phases resilience_testing

# Individual test components
test-setup:
	@echo "🔧 Setting up test environment..."
	python run_parallel_tests.py --setup-only

benchmark:
	pytest tests -v -m benchmark --benchmark-only --benchmark-autosave

security:
	safety check
	bandit -r mcp_server -f json -o bandit-report.json

docker:
	docker build -t mcp-server:latest .

release-smoke:
	$(UV_RUN) --extra dev python scripts/release_smoke.py --wheel --stdio

release-smoke-container:
	$(UV_RUN) --extra dev python scripts/release_smoke.py --container

alpha-dependency-sync:
	uv lock --locked
	uv sync --locked --extra dev --link-mode=copy

alpha-format-lint:
	$(UV_RUN) --extra dev black --check mcp_server tests
	$(UV_RUN) --extra dev isort --check-only mcp_server tests
	$(UV_RUN) --extra dev flake8 mcp_server tests
	$(UV_RUN) --extra dev pylint mcp_server --fail-under=7.0

alpha-mypy-ratchet:
	$(UV_RUN) --extra dev python scripts/check_mypy_baseline.py

alpha-unit-release-smoke:
	$(UV_RUN) --extra dev pytest tests/ \
		--ignore=tests/integration \
		--ignore=tests/test_git_integration.py \
		-n auto \
		--timeout=60 \
		--benchmark-skip \
		--no-cov
	$(MAKE) release-smoke

alpha-integration-smoke:
	SEMANTIC_SEARCH_ENABLED=false $(UV_RUN) --extra dev pytest tests/ -m "integration or slow" \
		--ignore=tests/test_git_integration.py \
		--ignore=tests/test_watcher.py \
		--timeout=120 \
		--no-cov \
		--benchmark-skip \
		-v

alpha-docs-truth:
	$(UV_RUN) --extra dev pytest \
		tests/test_release_metadata.py \
		tests/test_requirements_consolidation.py \
		tests/smoke/test_release_smoke_contract.py \
		tests/docs/test_p23_doc_truth.py \
		tests/docs/test_p25_release_checklist.py \
		tests/docs/test_p34_public_alpha_recut.py \
		-v --no-cov

alpha-production-matrix:
	$(UV_RUN) --extra dev pytest \
		tests/test_multi_repo_production_matrix.py \
		tests/test_multi_repo_failure_matrix.py \
		tests/test_repository_readiness.py \
		tests/test_tool_readiness_fail_closed.py \
		tests/test_git_index_manager.py \
		tests/test_ref_poller_edges.py \
		tests/test_rename_atomicity.py \
		tests/test_artifact_download.py \
		tests/smoke/test_release_smoke_contract.py \
		-v --no-cov

alpha-release-gates: alpha-dependency-sync alpha-format-lint alpha-mypy-ratchet alpha-unit-release-smoke alpha-integration-smoke alpha-docs-truth alpha-production-matrix

agent-doctor:
	$(UV_RUN) --extra dev python scripts/agent_validation.py doctor

agent-fast:
	$(UV_RUN) --extra dev python scripts/agent_validation.py run fast

agent-gate:
	$(UV_RUN) --extra dev python scripts/agent_validation.py run gate

agent-full:
	$(UV_RUN) --extra dev python scripts/agent_validation.py run full

agent-fix:
	$(UV_RUN) --extra dev python scripts/agent_validation.py run fix

agent-affected:
	$(UV_RUN) --extra dev python scripts/agent_validation.py affected

agent-fast-local: alpha-dependency-sync coverage-artifact-guard
	$(UV_RUN) --extra dev black --check \
		scripts/check_coverage_artifacts.py \
		scripts/agent_validation.py \
		tests/test_coverage_artifact_guard.py \
		tests/test_coverage_command_contract.py \
		tests/test_coverage_workflow_posture.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_coverage_docs.py \
		tests/docs/test_coverage_evidence_contract.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py
	$(UV_RUN) --extra dev isort --check-only \
		scripts/check_coverage_artifacts.py \
		scripts/agent_validation.py \
		tests/test_coverage_artifact_guard.py \
		tests/test_coverage_command_contract.py \
		tests/test_coverage_workflow_posture.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_coverage_docs.py \
		tests/docs/test_coverage_evidence_contract.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py
	$(UV_RUN) --extra dev flake8 \
		scripts/check_coverage_artifacts.py \
		scripts/agent_validation.py \
		tests/test_coverage_artifact_guard.py \
		tests/test_coverage_command_contract.py \
		tests/test_coverage_workflow_posture.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_coverage_docs.py \
		tests/docs/test_coverage_evidence_contract.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py
	$(UV_RUN) --extra dev pylint \
		scripts/check_coverage_artifacts.py \
		scripts/agent_validation.py \
		tests/test_coverage_artifact_guard.py \
		tests/test_coverage_command_contract.py \
		tests/test_coverage_workflow_posture.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_coverage_docs.py \
		tests/docs/test_coverage_evidence_contract.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py \
		--fail-under=7.0
	$(UV_RUN) --extra dev pytest \
		tests/test_coverage_artifact_guard.py \
		tests/test_coverage_command_contract.py \
		tests/test_coverage_workflow_posture.py \
		tests/test_release_metadata.py \
		tests/smoke/test_release_smoke_contract.py \
		tests/docs/test_p23_doc_truth.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_coverage_docs.py \
		tests/docs/test_coverage_evidence_contract.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py \
		-q --no-cov

agent-gate-local: agent-fast-local
	$(UV_RUN) --extra dev pytest \
		tests/test_sqlite_migrations.py tests/test_baml_contract.py \
		tests/test_tool_schema_handler_parity.py \
		tests/test_dispatcher.py::TestEnhancedDispatcherProtocolConformance::test_direct_symbol_response_satisfies_http_contract \
		tests/docs/test_p8_historical_sweep.py tests/test_workflow_action_pins.py \
		-q --no-cov
	$(MAKE) release-smoke
	SEMANTIC_SEARCH_ENABLED=false $(UV_RUN) --extra dev pytest \
		tests/test_multi_repo_production_matrix.py \
		tests/test_multi_repo_failure_matrix.py \
		tests/test_repository_readiness.py \
		tests/test_tool_readiness_fail_closed.py \
		tests/test_git_index_manager.py \
		tests/test_ref_poller_edges.py \
		tests/test_rename_atomicity.py \
		tests/test_artifact_download.py \
		-q --no-cov

agent-full-local: agent-gate-local coverage release-smoke-container

agent-fix-local:
	$(UV_RUN) --extra dev black \
		scripts/agent_validation.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py
	$(UV_RUN) --extra dev isort \
		scripts/agent_validation.py \
		tests/test_agent_validation.py \
		tests/test_localci_workflow_posture.py \
		tests/docs/test_localci_agent_validation_docs.py \
		tests/docs/test_localci_validation_contract.py

# Docker operations
docker-up:
	@echo "🐳 Starting Docker services..."
	docker-compose up -d
	@echo "✅ Services started. Check status with: make docker-health"

docker-dev:
	@echo "🐳 Starting Docker development environment..."
	@if [ -z "$$VOYAGE_API_KEY" ]; then \
		echo "⚠️  VOYAGE_API_KEY not set - semantic search will be disabled"; \
	fi
	docker-compose -f docker-compose.yml -f docker/compose/development/docker-compose.dev.yml up -d
	@echo "✅ Development environment started:"
	@echo "   🌐 MCP Server: http://localhost:8000"
	@echo "   📊 Prometheus: http://localhost:9090"
	@echo "   📈 Grafana: http://localhost:3000 (admin/dev)"
	@echo "   🗄️  Redis Commander: http://localhost:8081"

docker-prod:
	@echo "🐳 Starting Docker production environment..."
	@if [ ! -f .env.production ]; then \
		echo "❌ .env.production not found. Run: make setup-prod-env"; \
		exit 1; \
	fi
	docker-compose --env-file .env.production up -d
	@echo "✅ Production environment started"

docker-down:
	@echo "🐳 Stopping Docker services..."
	docker-compose down
	docker-compose -f docker-compose.yml -f docker/compose/development/docker-compose.dev.yml down

docker-logs:
	docker-compose logs -f

docker-health:
	@echo "🔍 Checking service health..."
	@echo "MCP Server:"
	@curl -s http://localhost:8000/health | grep -q '"status":"healthy"' && echo "  ✅ Healthy" || echo "  ❌ Unhealthy"
	@echo "Redis:"
	@docker-compose exec -T redis redis-cli ping 2>/dev/null | grep -q "PONG" && echo "  ✅ Healthy" || echo "  ❌ Unhealthy"
	@echo "Qdrant:"
	@curl -s http://localhost:6333/health 2>/dev/null | grep -q '"status":"ok"' && echo "  ✅ Healthy" || echo "  ❌ Unhealthy"

docker-test:
	@echo "🧪 Running tests in Docker..."
	docker-compose -f docker-compose.yml -f docker/compose/development/docker-compose.dev.yml run --rm test-runner

# Dormant features testing
test-dormant:
	@echo "🧪 Testing dormant features validation..."
	python run_parallel_tests.py --phases dormant_features_validation

test-real-world:
	@echo "🌍 Testing with real-world repositories..."
	python run_parallel_tests.py --phases real_world_validation --setup-real-world

test-semantic:
	@echo "🔍 Testing semantic search (requires VOYAGE_API_KEY)..."
	@if [ -z "$$VOYAGE_API_KEY" ]; then \
		echo "❌ VOYAGE_API_KEY environment variable not set"; \
		echo "   Get your API key from https://www.voyageai.com/"; \
		exit 1; \
	fi
	SEMANTIC_SEARCH_ENABLED=true pytest tests/real_world/test_semantic_search.py -v

test-redis:
	@echo "🗄️ Testing Redis caching (requires Redis server)..."
	@if ! command -v redis-cli >/dev/null 2>&1; then \
		echo "❌ Redis not available. Starting with Docker..."; \
		docker run -d --name test-redis -p 6379:6379 redis:7-alpine; \
		sleep 3; \
	fi
	REDIS_URL=redis://localhost:6379 pytest tests/real_world/test_redis_caching.py -v
	@if docker ps -q -f name=test-redis >/dev/null; then \
		docker stop test-redis && docker rm test-redis; \
	fi

test-advanced:
	@echo "🚀 Testing advanced indexing features..."
	pytest tests/real_world/test_advanced_indexing.py -v

test-cross-lang:
	@echo "🌐 Testing cross-language resolution..."
	pytest tests/real_world/test_cross_language.py -v

# Environment setup
setup-env:
	@echo "🔧 Setting up environment files..."
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "✅ Created .env from .env.example"; \
	else \
		echo "⚠️  .env already exists"; \
	fi

setup-dev-env: setup-env
	@echo "🔧 Setting up development environment..."
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@echo "🔑 To enable semantic search, add your Voyage AI API key:"
	@echo "   export VOYAGE_API_KEY='your-key'"
	@echo "   echo 'VOYAGE_API_KEY=your-key' >> .env"

setup-prod-env:
	@echo "🔧 Setting up production environment..."
	@if [ ! -f .env.production ]; then \
		cp .env.production.example .env.production; \
		echo "✅ Created .env.production from template"; \
		echo "🚨 IMPORTANT: Edit .env.production with production values:"; \
		echo "   - Set secure JWT_SECRET_KEY"; \
		echo "   - Set production VOYAGE_API_KEY"; \
		echo "   - Configure production database"; \
		echo "   - Set proper CORS_ORIGINS"; \
		echo "   - Change default passwords"; \
	else \
		echo "⚠️  .env.production already exists"; \
	fi

semantic-up:
	@echo "🚀 Starting local Qdrant..."
	docker compose -f docker-compose.qdrant.yml up -d qdrant

semantic-down:
	@echo "🛑 Stopping local Qdrant..."
	docker compose -f docker-compose.qdrant.yml down

semantic-preflight:
	python scripts/cli/mcp_cli.py setup semantic --dry-run

semantic-setup:
	python scripts/cli/mcp_cli.py setup semantic

# Data management
backup:
	@echo "💾 Creating backup..."
	@mkdir -p backup
	@if docker-compose ps | grep -q redis; then \
		docker run --rm -v mcp_redis-data:/data -v $(PWD)/backup:/backup alpine \
			tar czf /backup/redis-$(shell date +%Y%m%d-%H%M%S).tar.gz -C /data .; \
		echo "✅ Redis data backed up"; \
	fi
	@if docker-compose ps | grep -q qdrant; then \
		docker run --rm -v mcp_qdrant-data:/data -v $(PWD)/backup:/backup alpine \
			tar czf /backup/qdrant-$(shell date +%Y%m%d-%H%M%S).tar.gz -C /data .; \
		echo "✅ Qdrant data backed up"; \
	fi
	@if [ -f ./data/code_index.db ]; then \
		cp ./data/code_index.db ./backup/code_index-$(shell date +%Y%m%d-%H%M%S).db; \
		echo "✅ SQLite database backed up"; \
	fi
	@echo "💾 Backup completed in ./backup/"

restore:
	@echo "📥 Restore functionality - implement based on your backup files"
	@ls -la backup/ 2>/dev/null || echo "No backup directory found"

clean-docker:
	@echo "🧹 Cleaning up Docker resources..."
	docker-compose down -v --remove-orphans
	docker system prune -f
	@echo "✅ Docker cleanup completed"
