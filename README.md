# dreamer (agent-dreamer) 🌙

> **"인생은 뒤돌아보아야만 이해할 수 있지만, 살아가는 것은 앞으로 나아가야만 한다."** — 쇠렌 키에르케고르
> 
> 앤트로픽(Anthropic)의 연구 [「The components of agent memory」](https://www.anthropic.com/research/components-of-agent-memory)를 기반으로 구현된 코딩 에이전트(Claude Code, Cursor, Codex 등)용 **오프라인 수면 연산(Dreaming) 및 계층형 메모리 관리 엔진**입니다.

---

## 1. 주요 특징

- **바이브 코더를 위한 제로 컨피그 (Zero-friction):** 복잡한 설정 없이 `dreamer init` 한 줄이면 `MEMORY.md`, git `post-commit` 백그라운드 훅, 에이전트 지침이 자동 세팅됩니다.
- **다양한 프리셋 지원 (`--preset`):**
  - `vibe`: 기본 모드. 커밋할 때마다 백그라운드에서 조용히 꿈을 꾸며 불변 규칙을 정리합니다.
  - `strict`: Sonnet 모델 기반 엄격 검증 모드. 엔지니어링 ADR 및 불변 규칙 위반을 꼼꼼히 체크합니다.
  - `local`: 100% 오프라인 모드. Ollama 로컬 모델이나 휴리스틱 엔진을 사용해 외부 API 호출 없이 동작합니다.
  - `custom`: `dreamer.toml` 파일을 통해 메모리 상한선, 추출 규칙, LLM 제공자를 자유롭게 커스텀합니다.
- **다양한 LLM 프로바이더 지원:** Anthropic(Claude), OpenAI(GPT-4o), Ollama(로컬 Llama), OpenRouter/Groq, 오프라인 휴리스틱 모드(API 키 없이도 200줄 자동 관리).
- **멀티 에이전트 호환:** Claude Code (`CLAUDE.md`), Cursor (`.cursorrules`), Windsurf, Codex 세션 트레이스 로그 지원.
- **링크 무결성 검사 (`dreamer check`):** 200줄(25KB) 한도 초과 여부와 `memory/` 하위 토픽 문서의 깨진 링크(Broken pointer)를 자동 검사합니다.

---

## 2. 3계층 메모리 아키텍처 (3-Tier Layering)

1. **Tier 1: 최상위 인덱스 (`MEMORY.md`)**
   - 세션 시작 시 시스템 프롬프트에 자동 주입되는 파일.
   - **200줄(또는 25KB) 제한** 엄격 준수.
   - 상태(State)가 아닌 불변 규칙(Invariants)과 토픽 포인터(목차)만 유지.
2. **Tier 2: 도메인 지식 문서 (`memory/<topic>.md`)**
   - 아키텍처 결정(ADR), 특정 라이브러리 트릭, 버그 해결 이력 등.
   - 에이전트가 필요할 때 도구(`read_file`, `grep`)로 온디맨드 조회.
3. **Tier 3: 원시 트랜스크립트 (`.agent/traces/` 또는 git log)**
   - 세션 중 오간 도구 입출력과 에러 로그 원본. 실시간 세션에는 주입하지 않고 드리밍의 검증 재료로만 사용.

---

## 3. 빠른 시작 (Quickstart)

### 설치
```bash
git clone https://github.com/yulmucoffee/dreamer.git
cd dreamer
pip install -e .
```

### 레포지토리 초기화
작업 중인 프로젝트 폴더에서 실행합니다.

```bash
# 기본 바이브 모드 (Zero-config: MEMORY.md + git post-commit 훅 + CLAUDE.md + .cursorrules)
dreamer init

# 엄격 모드 (Strict)
dreamer init --preset strict

# 완전 로컬 오프라인 모드 (Ollama)
dreamer init --preset local

# 설정 파일(dreamer.toml) 생성 포함
dreamer init --with-config
```

### 수동 드리밍 및 검증
```bash
# 최근 커밋 기반 드리밍
dreamer run

# 변경 사항 미리보기 (dry-run)
dreamer run --dry-run

# 에이전트 세션 트레이스 로그 기반 드리밍
dreamer run --trace-file .agent/traces/session_123.jsonl

# 메모리 상태 및 깨진 링크 검사
dreamer check
```

---

## 4. 설정 (`dreamer.toml`)

`dreamer config init`으로 프로젝트 루트에 설정 파일을 생성할 수 있습니다.

```toml
preset = "vibe"

[memory]
index_file = "MEMORY.md"
topics_dir = "memory"
max_lines = 200
max_bytes = 25600

[model]
provider = "auto" # "auto" | "anthropic" | "openai" | "ollama" | "heuristic"
model = "claude-3-5-haiku-20241022"
api_key_env = "ANTHROPIC_API_KEY"
# base_url = "http://localhost:11434/v1" # 로컬 Ollama 사용 시

[hook]
hook_type = "post-commit"
run_background = true
quiet = true

[rules]
focus_areas = [
    "core_invariants",
    "runtime_gotchas",
    "architectural_decisions",
    "verified_bugfix_rules"
]
ignore_files = ["*.lock", "node_modules/**", "dist/**"]
```

---

## License
MIT License. Created by [@yulmucoffee](https://github.com/yulmucoffee).
