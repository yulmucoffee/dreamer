# dreamer 🌙

> **"인생은 뒤돌아보아야만 이해할 수 있지만, 살아가는 것은 앞으로 나아가야만 한다."** — 쇠렌 키에르케고르

앤트로픽 연구 [「The components of agent memory」](https://www.anthropic.com/research/components-of-agent-memory)의 **드리밍(dreaming)**을 코딩 에이전트 프로젝트에 적용하는 도구입니다. 세션이 끝나면 오프라인에서 그 세션을 되돌아보고, 다음 세션이 덜 헤매도록 프로젝트 메모리를 고칩니다. Claude Code, Codex, Cursor, Gemini CLI, Copilot 등 **하네스와 모델에 상관없이** 쓸 수 있습니다.

## 하네스 지원

| 하네스 | 메모리 주입 | 드리밍 입력 | 드리밍 실행 |
|---|---|---|---|
| Claude Code | `CLAUDE.md`의 `@memory/MEMORY.md` import | 세션 트랜스크립트 자동 수집, SessionEnd 훅 | API 엔진, 또는 에이전트가 직접 |
| Codex CLI | `AGENTS.md` 인라인 사본 | 세션 rollout 자동 수집 (`~/.codex/sessions`) | 에이전트가 직접 (`dreamer dream`) |
| Cursor, Copilot, Windsurf, Amp, opencode, Zed 등 AGENTS.md 계열 | `AGENTS.md` 인라인 사본 | `dreamer note` | 에이전트가 직접 |
| Gemini CLI | `GEMINI.md`의 `@memory/MEMORY.md` import | `dreamer note` | 에이전트가 직접 |
| 그 밖의 셸 접근 가능한 에이전트 | 지시 파일에 `memory/MEMORY.md` 안내 | `dreamer note` | 에이전트가 직접 |

- **API 엔진**(`dreamer run`): Claude API를 씁니다. 프리셋이나 멀티프로바이더 없이 Claude 전용입니다.
- **에이전트 주도 드리밍**(`dreamer dream …`): 어느 하네스의 어떤 모델이든, 사용자가 "dream"이라고 하면 에이전트가 직접 패스를 수행합니다. API 키가 필요 없고, 예산, 경로 제한, 검증, 스냅샷, 커서는 API 엔진과 같은 코드를 탑니다.
- **AGENTS.md는 인라인 사본입니다.** AGENTS.md에는 import 문법이 없기 때문입니다. 메모리가 바뀔 때마다 자동으로 동기화되고, 링크는 `memory/` 기준으로 고쳐집니다.
- **중복 주입 없음(기본 설정):** Claude Code는 `CLAUDE.md`가 있으면 기본적으로 `AGENTS.md`를 읽지 않습니다. Project instructions를 `claude-md-and-agents-md`로 바꾼 경우에만 인덱스가 두 번 들어갑니다.

## 동작 방식

```
에이전트 노트 (dreamer note, 모든 하네스)  +  세션 트랜스크립트 (Claude Code, Codex; 이 레포 것만)
   │  증분 커서: 각 세션은 한 번만 읽음
   ▼
다이제스트  ── 사고 블록·파일 덤프·성공 출력은 잘라내고, 사용자 발화·결정·에러는 남김 (실측 ~100×+ 압축)
   │  새 분량이 임계값 미만이면 API 호출 없음
   ▼
모델 (memory 도구로 편집 + 원문 검색으로 대조; Claude API 또는 하네스의 에이전트)
   │  스테이징 사본에서 작업 → 검증 → 원자적 적용
   ▼
memory/MEMORY.md   ← CLAUDE.md / GEMINI.md import, AGENTS.md 사본으로 매 세션 주입 (작게 유지)
memory/<topic>.md  ← 필요할 때 에이전트가 열어 봄
```

메모리 계층은 글을 따릅니다: **트랜스크립트**(검색 가능한 원본) → **토픽 노트**(링크로 연결된 위키) → **주입 컨텍스트**(`MEMORY.md`, 작게).

## 토큰 효율

- **주입 비용**: `MEMORY.md`는 기본 10KB(약 2.5k 토큰) 상한. 매 세션 반복되는 비용은 이것뿐입니다.
- **드리밍 입력**: 원문이 아니라 다이제스트, 그것도 새로 생긴 부분만. 한 번에 최대 60k자(약 15k 토큰)이고, 남은 분량은 다음 패스로 넘어갑니다.
- **편집 방식**: 인덱스 전체를 다시 쓰지 않고 `str_replace` 같은 부분 편집을 씁니다. 시스템 프롬프트와 도구 정의는 고정이라 턴 간 프롬프트 캐시가 적중합니다.
- **호출 생략**: 새 활동이 `min_new_chars` 미만이면 API를 부르지 않습니다.
- `dreamer status`가 패스별 input/cached/output 토큰을 보여 줍니다.

## 장기 프로젝트에서 망가지지 않게

| 위험 | 대책 |
|---|---|
| 메모리가 끝없이 커짐 (연구: 위키가 원본의 1.77배) | 인덱스·토픽별·전체 용량과 파일 수 **하드 상한**. 초과 쓰기는 도구 단계에서 거부되므로 병합·삭제로 자리를 만들어야 함 |
| 틀린 노트가 매 세션 전파 | 노트에 출처(날짜, 세션 id) 기록, 원문 대조 도구, 모순 시 수정·삭제하라는 목적 지시 |
| 실패 시 메모리 손상 | 스테이징 사본에서만 편집. 오류·거절·잘림·턴 초과 시 메모리와 커서 모두 그대로 |
| 세션 중 에이전트가 메모리를 동시에 수정 | 패스 시작과 끝의 해시를 비교해 다르면 적용하지 않고 다음에 재시도 |
| 훅 중복 실행 | 파일 락. 이미 돌고 있으면 건너뜀 |
| 잘못된 패스 | 적용 전 스냅샷(기본 20개). `dreamer undo`는 메모리와 커서를 함께 되돌려 다시 드리밍하게 함 |
| 반복 실패로 비용 누수 | 연속 3회 실패하면 자동 실행 중지, 수동 `dreamer run`으로 재개 |
| 깨진 링크, 고립 페이지 | 매 패스 검증 후 모델에 되먹임. `dreamer check`로도 확인 |
| 트랜스크립트 보존 기간 경과 (Claude Code 기본 30일, Codex는 오래된 세션을 `.zst`로 압축) | 세션 종료마다 드리밍하므로 그 전에 다이제스트됨. 지워지거나 압축된 파일은 커서에서 정리 |
| 하네스 간 메모리 불일치 | 적용, `undo`, `run` 때마다 AGENTS.md 사본을 동기화. `dreamer check`가 오래된 사본을 알려 줌 |

## 다른 레포와의 격리

- **읽기**: 레코드가 속한 작업 경로가 이 레포 안인지 경로 경계로 검사합니다. 이웃 레포(`proj`와 `proj-other`)도 걸러지고, 원문 검색도 같은 범위로 제한됩니다.
  - Claude Code: 레포 slug와 일치하는 디렉터리만 열고, 레코드마다 `cwd`를 확인합니다.
  - Codex: 세션 헤더의 `cwd`가 다른 레포면 그 한 줄만 읽고 해당 세션을 영구히 건너뜁니다.
- **쓰기**: 레포 안에만 씁니다.
  - `memory/`
  - `CLAUDE.md`의 관리 블록
  - `.claude/settings.local.json` (개인용, `.git/info/exclude`로 커밋 제외)
  - `AGENTS.md`, `GEMINI.md`의 관리 블록
  - `.dreamer/inbox/` (노트, `.git/info/exclude`로 커밋 제외)
  - `.git/dreamer/` (상태·로그·스냅샷, 워크트리별)

  `~/.claude/settings.json`이나 전역 git 설정은 건드리지 않습니다.
- **memory 도구**: 메모리 디렉터리 밖 경로, `..`, 심볼릭 링크, `.md`가 아닌 파일을 거부합니다.
- **팀원 보호**: dreamer가 설치되지 않은 팀원에게는 훅이 아무 일도 하지 않습니다. `@memory/MEMORY.md` import는 그대로 유용합니다.

## 빠른 시작

```bash
pip install -e .            # Python 3.10+
cd your-project
dreamer init                # memory/, CLAUDE.md·AGENTS.md 블록(+GEMINI.md가 있으면), SessionEnd 훅
git add memory CLAUDE.md AGENTS.md && git commit -m "chore: add project memory"
```

드리밍에는 Claude 자격 증명이 필요합니다: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, 또는 `ant auth login` 프로필.

```bash
dreamer run                 # Claude API로 패스 실행 (임계값 무시하려면 --force)
dreamer dream start         # API 없이, 지금 쓰는 에이전트가 직접 패스 실행 (출력된 안내를 따름)
dreamer note "pnpm만 사용; npm은 lockfile을 깨뜨림"   # 아무 하네스에서나 메모리 입력 남기기
dreamer sync                # MEMORY.md를 손으로 고친 뒤 AGENTS.md 사본 갱신
dreamer run --batches 10    # 기존 세션 백로그를 한 번에 처리
dreamer run --dry-run       # 적용·커서 이동 없이 미리보기
dreamer status              # 백로그, 용량, 최근 패스와 토큰 사용량
dreamer check               # 용량·링크·고립 페이지 검사
dreamer undo                # 마지막 패스 되돌리기
dreamer uninstall           # 블록·훅 제거 (메모리는 보존)
dreamer config              # dreamer.toml 예시 출력
```

`init` 옵션:
- `--harness claude,agents,gemini`: 주입 대상 지정
- `--git-hook`: 하네스와 무관하게 커밋마다 드리밍
- `--no-session-hook`: Claude Code SessionEnd 훅 생략
- `--with-config`: `dreamer.toml` 생성
기존 레이아웃(루트 `MEMORY.md`)은 `dreamer init` 실행 시 `memory/MEMORY.md`로 옮겨지고 링크도 고쳐집니다.

## 설정

모든 키는 선택 사항입니다. `dreamer config`로 기본값 전체를 볼 수 있고, 모르는 키나 삭제된 키(예: `preset`, `model.provider`)는 경고를 출력합니다.

```toml
[memory]
index_max_bytes = 10240    # 매 세션 주입 비용
store_max_bytes = 262144   # 전체 상한

[model]
model = "claude-opus-5-5"
effort = "high"

[dream]
min_new_chars = 3000       # 이보다 적으면 API 호출 없음
batch_chars = 60000
```

## 개발

```bash
python -m unittest discover -s tests
```

## License
MIT License. Created by [@yulmucoffee](https://github.com/yulmucoffee).
