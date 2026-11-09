# dreamer 🌙

> **"인생은 뒤돌아보아야만 이해할 수 있지만, 살아가는 것은 앞으로 나아가야만 한다."** — 쇠렌 키에르케고르

앤트로픽 연구 [「The components of agent memory」](https://www.anthropic.com/research/components-of-agent-memory)의 **드리밍(dreaming)**을 코딩 에이전트 프로젝트에 적용하는 도구입니다. 세션이 끝나면 오프라인에서 그 세션을 되돌아보고, 다음 세션이 덜 헤매도록 프로젝트 메모리를 고칩니다. Claude Code, Codex, Cursor, Gemini CLI, Copilot 등 **하네스와 모델에 상관없이** 쓸 수 있습니다.

## 한 사람, 여러 에이전트, 하나의 메모리

Claude Code, Codex(GPT), Cursor를 번갈아 써도 모두 같은 `memory/`를 읽고 같은 메모리에 기여합니다. Claude Code에서 알게 된 것이 다음 Cursor 세션에 주입되고, 그 반대도 마찬가지입니다. 에이전트를 하나만 쓰는 경우도 같은 구성의 일부일 뿐입니다.

```bash
dreamer init                          # 설치된 CLI를 보고 하네스를 자동 감지
dreamer init --harness codex          # 에이전트 하나만 (예: GPT/Codex만 쓸 때)
dreamer init --harness claude,codex,cursor
```

| 하네스 | 메모리 주입 | 세션 수집 | 자동 드리밍 트리거 |
|---|---|---|---|
| Claude Code | `CLAUDE.md`의 `@memory/MEMORY.md` import | 트랜스크립트 자동 수집 (`~/.claude/projects`) | SessionEnd 훅 (`.claude/settings.local.json`) |
| Codex CLI (GPT) | `AGENTS.md` 인라인 사본 | rollout 자동 수집 (`~/.codex/sessions`) | Stop 훅, 비동기 (`.codex/hooks.json`) |
| Cursor | `AGENTS.md` 인라인 사본 | 훅으로 기록: 프롬프트, 응답, 셸 명령과 출력, 편집 파일 (`.cursor/hooks.json`) | sessionEnd 훅 |
| Gemini CLI | `GEMINI.md`의 `@memory/MEMORY.md` import | `dreamer note` | `--git-hook` 또는 수동 |
| Copilot, Windsurf, Amp, opencode, Zed 등 | `AGENTS.md` 인라인 사본 (`--harness agents`) | `dreamer note`, 훅이 있으면 `dreamer capture generic` | `--git-hook` 또는 수동 |

훅 설정 파일은 모두 레포 안의 프로젝트 단위 파일입니다. 새로 만든 파일은 `.git/info/exclude`로 커밋에서 빠지고, 이미 추적 중인 파일에는 항목만 더합니다. dreamer가 없는 팀원에게 훅은 아무 일도 하지 않습니다.

### 개인 메모리 (레포마다 선택)

"한국어로 답하기"나 "적대적으로 검토하기" 같은 사람에 관한 기억은 프로젝트가 아니라 사람을 따라가야 합니다. `dreamer init --personal`(또는 `dreamer personal on`)을 한 레포들만 개인 저장소 `~/.dreamer/personal/`(`$DREAMER_HOME`으로 변경 가능)을 공유합니다.

- **드리밍:** 같은 패스에서 `/memories/personal/`도 편집합니다.
  - 모델에게는 "사람에 관한 것이고 어느 프로젝트에서나 참인 것만 개인 저장소에, 이 프로젝트·코드·사람·고객에 관한 것은 프로젝트에, 애매하면 프로젝트에"라고 지시합니다.
  - 개인 저장소에는 별도의 작은 예산이 있습니다(인덱스 4KB, 전체 64KB).
- **주입은 커밋되지 않는 로컬 파일로만:**

  | 하네스 | 파일 | 비고 |
  |---|---|---|
  | Claude Code | `CLAUDE.local.md` 관리 블록 | |
  | Codex | `AGENTS.override.md` | Codex는 이 파일이 있으면 `AGENTS.md` 대신 읽으므로 "AGENTS.md + 개인 블록"으로 생성하고, 동기화 때마다 다시 만듭니다. 직접 만든 override 파일이 있으면 블록만 추가합니다 |
  | Cursor | `.cursor/rules/dreamer-personal.mdc` | `alwaysApply` 규칙 |

  모두 `.git/info/exclude`로 제외하고, git이 추적 중인 파일에는 절대 쓰지 않습니다. 전역 설정(`~/.claude`, `~/.codex`, `~/.cursor`)은 건드리지 않습니다. Gemini CLI와 그 밖의 AGENTS.md 계열 도구는 프로젝트 로컬 파일이 없어 개인 메모리를 주입하지 못합니다.
- **격리:** 켜지 않은 레포는 개인 저장소를 읽지도 쓰지도 못합니다. 그런 레포에서 패스가 `/memories/personal`에 쓰려고 하면 거부됩니다.
- **공유 저장소 안전장치:**
  - 적용할 때 락을 잡고, 패스 시작 후 다른 레포가 개인 저장소를 바꿨으면 중단하고 다음에 재시도합니다.
  - `undo`는 그 뒤에 아무도 개인 저장소를 바꾸지 않았을 때만 되돌립니다.
  - 다른 레포의 로컬 사본은 그 레포의 다음 `run`이나 훅, 또는 `dreamer sync` 때 갱신됩니다.
- **주의:** 개인 저장소는 켠 레포 모두에 주입됩니다. 고객별로 비밀 유지가 필요한 레포들 사이에서는 켜지 않는 것이 안전합니다.

### 채팅 앱과 MCP 클라이언트 (`dreamer mcp`)

Claude Desktop 같은 채팅 앱은 레포 파일을 읽지 못합니다. 그래서 같은 메모리를 MCP(stdio)로도 제공합니다.

```bash
dreamer mcp --print-config                 # 채팅 앱용: 개인 메모리만
dreamer mcp --print-config --repo-dir .    # 이 프로젝트 + (켰다면) 개인 메모리
```

- **도구:** `memory_recall`(인덱스), `memory_read`(페이지), `memory_search`, `memory_note`(`about`: `me` 또는 `project`).
- **페이지는 읽기 전용:** 쓰기는 노트로만 받고, 다음 드리밍이 통합합니다. 에이전트마다 직접 덧붙이게 하면 중복과 오류가 모든 에이전트에 퍼지기 때문입니다. 검증, 예산, 실패 시 무기록 규칙은 그대로 적용됩니다.
- **범위:** 개인 메모리를 켜지 않은 레포에서 띄운 서버는 개인 메모리를 노출하지 않습니다.
- **개인 전용 패스:** 채팅 앱이 남긴 개인 노트는 `dreamer run --personal`(또는 `dreamer dream --personal start`)로 정리합니다. 개인 메모리를 켠 레포의 다음 `run`이나 훅이 노트가 쌓여 있으면 자동으로 이 패스도 실행합니다.
- **ChatGPT:** 원격(HTTPS) MCP만 지원하므로 이 로컬 서버를 직접 붙일 수 없습니다. GPT는 Codex CLI로 참여합니다.

[Notion의 공유 메모리](https://x.com/NotionHQ/status/2118059335930744999)와 같은 문제("모든 에이전트에 하나의 두뇌")를 다른 방식으로 풉니다.

| | Notion 공유 메모리 | dreamer |
|---|---|---|
| 저장 위치 | 호스팅되는 Notion 워크스페이스 | 레포의 `memory/`(git으로 버전 관리) + 로컬 개인 저장소 |
| 채팅 앱 | ChatGPT, Claude 웹 (원격 MCP) | Claude Desktop 등 로컬 MCP 클라이언트 |
| 쓰기 | 에이전트가 직접 저장, 하루 1회 점검 작업 | 노트·트랜스크립트 → 드리밍이 원문과 대조하며 통합 |
| 근거 | 대화 로그를 저장하지 않음 | 세션 트랜스크립트로 노트를 검증 |
| 크기 관리 | 수동 검토 권장 | 하드 예산, 링크·고립 페이지 검증 |
| 공유 스킬 | 지원 | 아직 없음 |

### 누가 드리밍을 하나 (엔진)

자동 패스의 모델 역할은 `[dream] engine`이 정합니다. Anthropic API 키가 없어도, 이미 쓰고 있는 에이전트 구독으로 돌아갑니다.

| engine | 실행 | 필요한 것 |
|---|---|---|
| `api` | 내장 루프, Claude API | Anthropic 자격 증명 |
| `claude-code` | `claude -p` (헤드리스) | Claude 구독 |
| `codex` | `codex exec --full-auto` | ChatGPT/OpenAI 로그인 |
| `cursor` | `cursor-agent -p --force` | Cursor 플랜 |
| `auto` (기본) | Anthropic 자격 증명이 있으면 `api`, 없으면 PATH에 있는 첫 CLI (claude → codex → cursor-agent) | |
| `none` | 자동 드리밍 끔. 에이전트에게 "dream"이라고 말해 직접 실행 | |

- **CLI 엔진은 대화형 "dream"과 같은 프로토콜**(`dreamer dream start … finish`)을 탑니다. 예산, 검증, 스냅샷, 커서가 어떤 모델이든 같게 적용됩니다. 끝내지 못하면 아무것도 바뀌지 않고 실패로 기록됩니다.
- **세부 설정:** `engine_model`(예: `haiku`)로 모델을, `engine_command`로 다른 에이전트 CLI를 지정할 수 있습니다.
- **재귀 방지:** 엔진은 `DREAMER_DREAMING=1` 환경 변수로 실행되고, 모든 dreamer 훅은 이 값을 보면 즉시 종료합니다. 패스 프롬프트에는 마커가 들어 있어서, 드리밍 세션 자신의 트랜스크립트는 다음 패스에 들어가지 않습니다.
- **주입 방식:** AGENTS.md에는 import 문법이 없어서 인라인 사본을 넣습니다. 메모리가 바뀔 때마다 동기화되고 링크는 `memory/` 기준으로 고쳐집니다.
- **중복 주입 없음:** Claude Code는 `CLAUDE.md`가 있으면 기본적으로 `AGENTS.md`를 읽지 않습니다.

## 동작 방식

```
세션 기록: Claude Code·Codex 트랜스크립트, Cursor 훅 기록, dreamer note  (이 레포 것만)
   │  증분 커서: 각 세션은 한 번만 읽음
   ▼
다이제스트  ── 사고 블록·파일 덤프·성공 출력은 잘라내고, 사용자 발화·결정·에러는 남김 (실측 ~100×+ 압축)
   │  새 분량이 임계값 미만이면 API 호출 없음
   ▼
모델 (memory 도구로 편집 + 원문 검색으로 대조; Claude API 또는 claude / codex / cursor 에이전트)
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
  - `.claude/settings.local.json`, `.codex/hooks.json`, `.cursor/hooks.json`의 dreamer 항목
  - `.dreamer/`: 상태, 로그, 스냅샷, 노트, Cursor 세션 기록. 워크트리별이고 `.git/info/exclude`로 커밋에서 제외됩니다. `.git/` 아래가 아닌 이유는 Codex 샌드박스가 `.git/`을 읽기 전용으로 두기 때문입니다.

  `~/.claude/settings.json`이나 전역 git 설정은 건드리지 않습니다.
- **memory 도구**: 메모리 디렉터리 밖 경로, `..`, 심볼릭 링크, `.md`가 아닌 파일을 거부합니다.
- **팀원 보호**: dreamer가 설치되지 않은 팀원에게는 훅이 아무 일도 하지 않습니다. `@memory/MEMORY.md` import는 그대로 유용합니다.

## 빠른 시작

```bash
pip install -e .            # Python 3.10+
cd your-project
dreamer init                # 하네스 감지 → memory/, 지시 파일 블록, 훅
git add memory CLAUDE.md AGENTS.md && git commit -m "chore: add project memory"
```

드리밍에는 Claude 자격 증명이 필요합니다: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, 또는 `ant auth login` 프로필.

```bash
dreamer run                 # 엔진으로 패스 실행 (임계값 무시: --force, 엔진 지정: --engine codex)
dreamer dream start         # API 없이, 지금 쓰는 에이전트가 직접 패스 실행 (출력된 안내를 따름)
dreamer note "pnpm만 사용; npm은 lockfile을 깨뜨림"   # 아무 하네스에서나 메모리 입력 남기기
dreamer sync                # MEMORY.md를 손으로 고친 뒤 AGENTS.md·개인 사본 갱신
dreamer personal on|off|show   # 이 레포에서 개인 메모리 켜기/끄기/보기
dreamer note --personal "..."  # 나에 관한 노트 (채팅 앱은 MCP memory_note)
dreamer run --personal         # 개인 노트만 드리밍
dreamer mcp                    # MCP 서버 (채팅 앱, 기타 MCP 클라이언트)
dreamer run --batches 10    # 기존 세션 백로그를 한 번에 처리
dreamer run --dry-run       # 적용·커서 이동 없이 미리보기
dreamer status              # 백로그, 용량, 최근 패스와 토큰 사용량
dreamer check               # 용량·링크·고립 페이지 검사
dreamer undo                # 마지막 패스 되돌리기
dreamer uninstall           # 블록·훅 제거 (메모리는 보존)
dreamer config              # dreamer.toml 예시 출력
```

`init` 옵션:
- `--harness claude,codex,cursor,gemini,agents`: 대상 하네스 지정 (기본: 자동 감지)
- `--git-hook`: 하네스와 무관하게 커밋마다 드리밍
- `--no-hooks`: 세션 수집과 자동 드리밍 훅 생략
- `--with-config`: `dreamer.toml` 생성
- `--personal`: 개인 메모리를 이 레포와 공유 (로컬 파일로만 주입)
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
