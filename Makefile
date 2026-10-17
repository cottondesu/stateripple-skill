PYTHON ?= python3
AGENT ?=
SUITE ?= release
PROFILE ?= snapshot-simulation
EVIDENCE ?=
MANUAL_REVIEW ?=
HUMAN_REVIEW ?=
AI_REVIEW ?=
OUTPUT ?=

.PHONY: build check test cli-compat release-check eval-live eval-replay eval-coverage eval-release eval-profile

build:
	$(PYTHON) scripts/build.py

check:
	$(PYTHON) scripts/validate.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

cli-compat:
	$(PYTHON) scripts/check_cli_compat.py

release-check:
	$(PYTHON) scripts/release_check.py

eval-live:
	@test -n "$(AGENT)" || { echo 'AGENT=codex or AGENT=claude is required' >&2; exit 2; }
	$(PYTHON) scripts/eval_live.py --agent "$(AGENT)" --suite "$(SUITE)"

eval-replay:
	@test -n "$(EVIDENCE)" || { echo 'EVIDENCE=/path/to/live/run is required' >&2; exit 2; }
	$(PYTHON) scripts/eval_replay.py --evidence "$(EVIDENCE)" $(if $(MANUAL_REVIEW),--manual-review "$(MANUAL_REVIEW)",) $(if $(OUTPUT),--output-dir "$(OUTPUT)",)

eval-coverage:
	$(PYTHON) scripts/eval_coverage.py --profile "$(PROFILE)" $(if $(EVIDENCE),--evidence "$(EVIDENCE)",) $(if $(OUTPUT),--output "$(OUTPUT)",)

eval-release:
	@test -n "$(EVIDENCE)" || { echo 'EVIDENCE="live-or-replay-dir ..." is required' >&2; exit 2; }
	$(PYTHON) scripts/eval_coverage.py --release $(foreach path,$(EVIDENCE),--evidence "$(path)") $(if $(HUMAN_REVIEW),--human-review "$(HUMAN_REVIEW)",) $(if $(AI_REVIEW),--ai-review "$(AI_REVIEW)",) $(if $(OUTPUT),--output "$(OUTPUT)",)

eval-profile:
	$(PYTHON) scripts/eval_profile.py --profile "$(PROFILE)"
