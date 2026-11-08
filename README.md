# dreamer (agent-dreamer)

> **"인생은 뒤돌아보아야만 이해할 수 있지만, 살아가는 것은 앞으로 나아가야만 한다."** — 쇠렌 키에르케고르
> 
> 앤트로픽(Anthropic)의 연구 [「The components of agent memory」](https://www.anthropic.com/research/components-of-agent-memory)를 기반으로 구현된 코딩 에이전트(Claude Code, Codex, CLI Agents)용 오프라인 수면 연산(Dreaming) 및 계층형 메모리 관리 엔진입니다.

---

## 1. 왜 거대 컨텍스트 창 대신 '드리밍'인가?

1M, 2M 토큰의 컨텍스트 창이 있어도 모든 히스토리를 쏟아붓는 에이전트는 무너집니다.
- **소넷 3.7의 실패 (포켓몬스터 레드):** 일기처럼 모든 상태를 파일 413개에 적다가 자기 메모리에 질식해 멈춤.
- **오푸스 5.5의 성공:** 상태값은 화면으로 보고, 검증된 불변 규칙(Invariants) 9개만 `global.md`에 남겨 게임 클리어.
- **달맞이산 벼랑 탈출:** 세션 중 발생한 착각("포켓몬센터는 동쪽에 있다")을 세션 종료 후 원시 로그(Ground Truth)와 대조하는 **드리밍(Dreaming)**을 통해 단 한 줄 수정으로 극복.
- **200줄 룰:** Claude Code처럼 매 세션 시작 시 `MEMORY.md`의 상단 200줄(25KB) 인덱스만 주입하여 컨텍스트 낭비와 환각을 방지.

`dreamer`는 에이전트가 코딩 중에는 가볍게 달리고, 커밋이나 세션 종료 후 백그라운드에서 조용히 꿈을 꾸며 지식을 200줄 이내로 정제하는 비동기 루프를 제공합니다.

---

## 2. 3계층 아키텍처 (3-Tier Layering)

1. **Tier 1: 최상위 인덱스 (`MEMORY.md`)**
   - 세션 시작 시 시스템 프롬프트로 주입되는 파일.
   - 최대 200줄 / 25KB 제한 엄격 적용.
   - 핵심 불변 규칙(Invariants)과 하위 지식 포인터만 유지.
2. **Tier 2: 도메인 지식 문서 (`memory/<topic>.md`)**
   - 아키텍처 결정(ADR), 특정 라이브러리 트릭, 디버깅 로그 등.
   - 에이전트가 필요할 때 도구(`read_file`, `grep`)로 온디맨드 조회.
3. **Tier 3: 원시 트랜스크립트 (`.agent/traces/` 또는 git log)**
   - 세션 중 오간 도구 입출력과 에러 로그 원본. 실시간 세션에는 주입하지 않고 드리밍의 검증 재료로만 사용.

---

## 3. 빠른 시작 (Quickstart)

### 설치
```bash
pip install -e .
```

### 레포지토리 초기화
작업 중인 프로젝트 폴더에서 한 번만 실행합니다.
```bash
cd /path/to/your/repo
dreamer init
```
- 프로젝트 루트에 `MEMORY.md` 템플릿과 `memory/` 폴더를 생성합니다.
- `.git/hooks/post-commit`을 설치하여, 커밋할 때마다 백그라운드로 `dreamer run`이 자동 실행됩니다.

### 수동 드리밍 실행
```bash
# 최근 git 커밋/diff 기반 드리밍
dreamer run

# 세션 트랜스크립트 파일 지정
dreamer run --trace-file .agent/traces/session_123.jsonl
```

### 메모리 한도 검사 (200줄 / 25KB)
```bash
dreamer check
```

---

## 4. 환경 변수
- `ANTHROPIC_API_KEY`: 드리밍 시 사용할 Anthropic API 키 (설정되지 않으면 200줄 한도 강제 관리 휴리스틱 모드로 동작합니다).
- `DREAMER_MODEL`: 드리밍용 모델 (기본값: `claude-3-5-haiku-20241022`).

---

## License
MIT License. Created by [@yulmucoffee](https://github.com/yulmucoffee).
