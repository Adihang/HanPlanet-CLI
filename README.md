# HanHarness

[OpenHarness](https://github.com/HKUDS/OpenHarness) 포크 — Hanplanet 서비스에 맞게 커스텀한 AI 코딩 어시스턴트 CLI.

---

## 설치

### 요구사항

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) — 패키지 관리
- Node.js 18+ — TUI 프론트엔드 빌드

### 개발 환경 세팅

```bash
git clone https://github.com/Adihang/HanHarness.git
cd HanHarness

# Python 의존성 설치
pip install uv
uv --version

uv sync --extra dev

# TUI 프론트엔드 의존성 설치
cd frontend/terminal && npm ci && cd ../..
```

### 전역 설치 (pipx)

```bash
pip install pipx
pipx ensurepath
pipx --version

pipx install -e .
pipx upgrade HanHarness
```

설치 후 `hanplanet` or `oh` 명령어로 실행.

> **개발 중 소스 코드 직접 참조 (pipx 환경)**
>
> pipx venv에서 소스 코드 변경이 즉시 반영되려면 `.pth` 파일을 추가한다.
>
> ```bash
> # 경로 예시 (Python 버전에 맞게 수정)
> echo "/path/to/HanHarness
> /path/to/HanHarness/src" \
>   > ~/.local/pipx/venvs/hanharness/lib/python3.13/site-packages/_hanharness.pth
> ```

### 실행

```bash
uv run hanplanet          # 개발 환경 (TUI)
hanplanet                 # pipx 전역 설치 후
hanplanet -p "질문"       # 비대화형 모드
```

---

## 주요 커스텀 내용

원본 OpenHarness 대비 변경된 사항.

### Hanplanet OAuth 인증 연동

**파일:** `src/openharness/ui/backend_host.py`

원본의 PKCE 방식 대신 Hanplanet 서버(`hanplanet.com`)와 연동하는 폴링 기반 JWT 인증 흐름으로 교체.

**인증 흐름:**
1. 클라이언트가 `https://hanplanet.com/login/handrive?state=HEX&client_name=HanHarness+CLI` 를 브라우저로 열기
2. 사용자가 브라우저에서 연결/취소 클릭
3. 클라이언트가 `https://hanplanet.com/api/sync/auth/handrive-callback?state=HEX` 를 2초 간격으로 폴링
4. 서버 응답:
   - `202` — 대기 중
   - `200 {"access_token": ..., "refresh_token": ...}` — 인증 완료
   - `200 {"status": "cancelled"}` — 사용자 취소

**Access Token 자동 갱신:**
- 매 쿼리 실행 전 JWT `exp` 클레임 확인
- 만료 5분 전부터 `POST /api/sync/auth/refresh` 로 자동 갱신
- 갱신된 토큰을 저장소에 저장하고 엔진 클라이언트 재초기화

**크리덴셜 저장 네임스페이스:** `profile:hanplanet`
- `api_key` — access token (JWT)
- `refresh_token` — refresh token (JWT, 30일)

---

### Provider 목록 커스텀 및 커스텀 API 추가

**파일:** `src/openharness/ui/backend_host.py`

- `/provider` 목록에 등록된 모든 프로바이더 표시 (활성→설정됨→커스텀→미설정 순 정렬)
- `openrouter` 등 불필요한 내장 프로바이더 숨김 처리 (`_HIDDEN_PROFILES`)
- Hanplanet 선택 시 항상 인증 방법 선택 화면 표시 (계정 전환 지원)
- 프로바이더 선택 완료 후 자동으로 모델 선택 화면 이동
- `➕ 커스텀 API 추가` 옵션으로 OpenAI-compatible 엔드포인트 등록 지원

**커스텀 API 추가 흐름:**
1. `/provider` → `➕ 커스텀 API 추가` 선택
2. 인증 방식 체크박스 선택 (복수 선택 가능): `🌐 OAuth` / `🔑 API 키`
3. Base URL → (OAuth이면) OAuth 로그인 URL + 폴링 URL → (API 키이면) API 키 입력
4. 기본 모델명, 프로파일 이름, 표시 이름 입력
5. 저장 후 즉시 해당 프로바이더로 전환

**커스텀 프로바이더 관리:**
- `/provider` 에서 등록된 커스텀 프로바이더 선택 시 관리 메뉴 표시
- 등록 시 선택한 인증 방식에 따라 메뉴 구성:
  - `🌐 OAuth 재인증` (OAuth로 등록한 경우)
  - `🔑 API 키 변경` (API 키로 등록한 경우)
  - `🗑 프로바이더 삭제`
- 삭제 시 크리덴셜 포함 완전 제거, 활성 프로파일이면 `claude-api`로 자동 전환

---

### Hanplanet 모델 목록 동적 조회

**파일:** `src/openharness/ui/backend_host.py`

- `/model` 명령어 실행 시 Hanplanet 프로파일이 활성 상태면 `GET /ai/v1/models` API로 실시간 조회
- API 조회 실패 시 현재 선택된 모델만 표시 (GPT 등 타 프로바이더 목록으로 fallback 방지)
- 모델 조회 401 시 refresh token으로 재발급 후 재시도

---

### Hanplanet 프로파일 설정

**파일:** `src/openharness/ui/backend_host.py`

OAuth 또는 API키 입력 완료 시 `~/.openharness/settings.json`에 저장되는 프로파일:

```json
{
  "label": "Hanplanet",
  "provider": "openai",
  "api_format": "openai",
  "auth_source": "openai_api_key",
  "base_url": "https://hanplanet.com/ai/v1",
  "credential_slot": "hanplanet"
}
```

`active_profile`을 `"hanplanet"`으로 즉시 전환 (`use_profile` 호출).

---

### 어시스턴트 이름 변경

**파일:** `src/openharness/prompts/system_prompt.py`

시스템 프롬프트에서 자기 소개 이름을 `OpenHarness` → `HanHarness` 로 변경.

---

### 배너 변경

**파일:** `src/openharness/ui/backend_host.py`

시작 시 표시되는 ASCII 배너를 HANPLANET 6행 아트로 교체, 하단에 `www.hanplanet.com` 표기.

---

### 슬래시 커맨드 인터랙티브 메뉴

원본은 텍스트 출력 방식이던 여러 커맨드를 TUI 네이티브 선택 메뉴로 교체:

| 커맨드 | 변경 내용 |
|--------|-----------|
| `/provider` | 선택 목록 UI |
| `/model` | 선택 목록 UI (프로바이더별 동적 목록) |
| `/permissions` | 선택 목록 UI |
| `/config` | 선택 목록 UI |
| `/language` | 선택 목록 UI |
| `/memory` | 선택 목록 UI |
| `/plugin` | 선택 목록 UI |
| `/agents` | 선택 목록 UI |
| `/skills` | 선택 목록 UI |
| `/resume` | 선택 목록 UI (이전 세션 목록, 최대 20개) |

---

### `/resume` 세션 복구

**파일:** `src/openharness/ui/backend_host.py`

`/resume` 입력 시 저장된 세션 목록을 인터랙티브 선택 메뉴로 표시.

- 세션 없을 때 `"저장된 세션이 없습니다."` 메시지 출력
- 항목당 summary(label) + 날짜·메시지 수·모델(description) 분리 표시
- 최대 20개 세션 표시
- ↑↓ 이동, ⏎ 선택, esc 취소

---

### Ollama 로컬 프로바이더 지원

- 기본 base URL에 `/v1` suffix 적용
- API 키 없이 로컬 Ollama 서버 연결 지원
- 로컬/원격 Ollama 모델 목록 분리 표시

---

### Ollama OOM 스트림 중단 자동 재시도

**파일:** `src/openharness/api/openai_client.py`

로컬 모델이 응답 생성 중 메모리 부족 등으로 연결을 끊을 때 자동으로 재시도.

- 아래 에러 메시지를 retryable로 처리 (최대 3회, 지수 백오프):
  - `peer closed connection without sending complete message body`
  - `incomplete chunked read`
  - `server disconnected`, `connection reset`
- **재시도 시 중복 텍스트 방지:** 실패한 시도에서 나온 텍스트 델타를 버퍼에 보관하다가 폐기, 성공한 시도의 결과만 방출
- **히스토리 오염 방지:** tool call 없는 빈 assistant 응답을 `content: null` 대신 `content: ""`로 저장 → 이후 요청 400 에러 방지

---

### TUI 레이아웃 하단 여백 수정

**파일:** `frontend/terminal/src/App.tsx`

터미널 크기를 늘렸을 때 대화 내용과 입력창 사이에 과도한 빈 공간이 생기는 문제 수정.

- 원인: 최상위 `Box`에 `height="100%"` 설정 → `ConversationView(flexGrow=1)`이 터미널 전체 높이를 점유
- 수정: `height="100%"` 제거 → 내용 높이에 맞게 렌더링

---

### 스피너 개선 (ink-spinner 교체)

**파일:** `frontend/terminal/src/components/Spinner.tsx`, `frontend/terminal/package.json`

Ink v5에서 `setInterval+useState` 방식은 외부 이벤트 사이에 렌더를 flush하지 않아 스피너가 터미널 입력/리사이즈 시에만 움직이는 버그가 있었음.

- `ink-spinner@5.0.0` 패키지로 교체 → 항상 부드럽게 애니메이션
- AI 대기 상태에서 `label` prop이 없을 때 커스텀 VERBS (`Thinking…` / `Processing…` / `Analyzing…` 등) 순환 표시
- 도구 실행 중엔 `Running <tool-name>...` 고정 표시

---

### 슬래시 커맨드 피커 입력 개선

**파일:** `frontend/terminal/src/App.tsx`

- **Backspace로 피커 닫기:** `/` 입력 후 커맨드 목록이 열린 상태에서 backspace로 `/`를 삭제하면 일반 입력 모드로 복귀. 기존에는 TextInput이 키 이벤트를 받지 못해 동작하지 않았음.
- **실시간 필터링 유지:** 피커가 열린 상태에서 추가로 문자를 입력하면 목록이 실시간 필터링됨.
- **2단계 선택 모달 freeze 수정:** `/tasks 출력 보기`, `/memory show`, `/plugin enable` 등 2단계로 열리는 선택 모달에서 ESC로 취소하면 UI가 멈추는 버그 수정 (line_complete 누락 원인).

---

### preload_skills — 로컬 모델 스킬 지원

**파일:** `src/openharness/config/settings.py`, `src/openharness/prompts/context.py`, `src/openharness/commands/registry.py`

Ollama/Hanplanet API 등 tool call을 신뢰성 있게 수행하지 못하는 로컬 모델을 위해 스킬 내용을 시스템 프롬프트에 직접 주입하는 기능 추가.

**설정 방법 (`~/.openharness/settings.json`):**
```json
{
  "preload_skills": ["*"]
}
```
`["*"]` → 모든 스킬 preload, `["commit", "review-pr"]` → 특정 스킬만 preload.

**런타임 관리 커맨드:**
```
/skills preload *         # 모든 스킬 preload
/skills preload commit    # 특정 스킬 preload
/skills unload commit     # preload 해제
/skills list              # 현재 상태 확인 (* = preloaded)
```

---

### API 에러 메시지 개선

**파일:** `src/openharness/api/openai_client.py`, `src/openharness/engine/query.py`

OpenAI SDK 예외에서 `str(exc)`가 빈 문자열을 반환하는 경우(`"API error: "`) 발생하던 문제 수정.

- `exc.body["error"]` / `exc.body["message"]` → `response.text` → `repr(exc)` 순으로 fallback
- HTTP 상태 코드를 메시지 앞에 붙여 어떤 오류인지 명확히 표시 (`HTTP 401: ...`)

---

## 설정 파일

런타임 설정은 `~/.openharness/settings.json`에 저장된다.

```jsonc
{
  "active_profile": "hanplanet",   // 현재 활성 프로바이더
  "profiles": {
    "hanplanet": { ... },          // Hanplanet 프로파일 (OAuth 후 자동 생성)
    "ollama": { ... }              // Ollama 프로파일
  }
}
```

크리덴셜(토큰/키)은 별도 파일(`~/.openharness/credentials.json`) 또는 시스템 keyring에 저장.

---

## 업스트림 동기화

### 기본 절차

```bash
# 원격 구성 (최초 1회)
# origin  → 포크 (푸시 대상)
# upstream → 원본 (풀 전용)
git remote add upstream https://github.com/HKUDS/OpenHarness.git

git fetch upstream
git merge upstream/main
```

### 충돌 발생 시 우선순위 원칙

충돌이 발생한 파일에 따라 아래 기준으로 처리한다.

#### 우리 버전(ours)을 유지해야 하는 파일

아래 파일들은 우리 커스텀 내용이 핵심이므로 충돌 시 **우리 버전을 우선**한다.  
포함된 커스텀 함수/섹션은 절대 upstream으로 덮어쓰지 않는다.

| 파일 | 보호해야 할 커스텀 내용 |
|------|------------------------|
| `src/openharness/ui/backend_host.py` | `_hanplanet_oauth_flow`, `_hanplanet_save_and_select`, `_hanplanet_refresh_token`, `_maybe_refresh_hanplanet_token`, `_fetch_hanplanet_models`, `command == "model-for-hanplanet"`, `command == "provider"` 내 Hanplanet 분기, `command == "model"` 내 Hanplanet 분기, `_VISIBLE_PROFILES`, ASCII 배너 |
| `src/openharness/prompts/system_prompt.py` | `_BASE_SYSTEM_PROMPT` 첫 줄 (`You are HanHarness`) |
| `pyproject.toml` | `[project.scripts]` 의 `hanplanet` entry point |

충돌 해결 명령어 예시 (파일 전체를 우리 버전으로):

```bash
git checkout --ours src/openharness/prompts/system_prompt.py
git checkout --ours pyproject.toml
git add src/openharness/prompts/system_prompt.py pyproject.toml
```

`backend_host.py`는 원본 로직도 포함되어 있으므로 파일 전체를 `--ours`로 처리하지 말고 **충돌 구간을 직접 확인하며 병합**한다

---

## 내부 구조

### 디렉토리 전체 구성

```
HanHarness/
├── src/openharness/          # 메인 Python 패키지
│   ├── api/                  # LLM 프로바이더 클라이언트
│   ├── auth/                 # 인증 및 크리덴셜 관리
│   ├── bridge/               # 서브세션 스포닝
│   ├── channels/             # 메시지 채널 (Slack, Discord 등)
│   ├── commands/             # 슬래시 커맨드 레지스트리 (54개+)
│   ├── config/               # 설정 스키마 및 경로
│   ├── coordinator/          # 멀티에이전트 코디네이터 모드
│   ├── engine/               # 에이전트 루프 핵심
│   ├── hooks/                # PreToolUse / PostToolUse 훅
│   ├── keybindings/          # 키바인딩 로드 및 파싱
│   ├── mcp/                  # Model Context Protocol 클라이언트
│   ├── memory/               # 프로젝트 영속 메모리
│   ├── output_styles/        # 출력 스타일 (default, codex 등)
│   ├── permissions/          # 도구 실행 권한 체크
│   ├── personalization/      # 사용자 선호도 추출
│   ├── plugins/              # 플러그인 로더/인스톨러
│   ├── prompts/              # 시스템 프롬프트 조립
│   ├── sandbox/              # Docker 샌드박스 실행 환경
│   ├── services/             # 크론 스케줄러, 세션 스토리지
│   ├── skills/               # .md 기반 온디맨드 지식 로더
│   ├── state/                # 런타임 앱 상태
│   ├── swarm/                # 멀티에이전트 팀 스포닝
│   ├── tasks/                # 백그라운드 태스크 관리
│   ├── themes/               # TUI 테마 정의
│   ├── tools/                # 44개+ 도구 구현
│   ├── ui/                   # React TUI 연동 및 백엔드 호스트
│   ├── utils/                # 파일락, 네트워크 가드 등 유틸
│   ├── vim/                  # Vim 모달 입력 모드
│   ├── voice/                # 음성 입력 (STT)
│   ├── cli.py                # `oh` / `hanplanet` CLI 진입점
│   └── __main__.py           # `python -m openharness` 진입점
├── ohmo/                     # 개인 에이전트 CLI (ohmo)
│   ├── gateway/              # 멀티채널 게이트웨이
│   ├── cli.py                # `ohmo` CLI 진입점
│   ├── runtime.py            # 세션 실행
│   └── workspace.py          # ohmo 워크스페이스 구조
├── frontend/terminal/        # React + Ink TUI 프론트엔드
│   └── src/
│       ├── components/       # UI 컴포넌트 (17개)
│       ├── hooks/            # useBackendSession 훅
│       ├── theme/            # 테마 컨텍스트
│       ├── App.tsx           # 최상위 앱 컴포넌트
│       ├── index.tsx         # TUI 진입점
│       └── types.ts          # TypeScript 타입 정의
└── tests/                    # 테스트 스위트 (37개 모듈)
```

---

### 동작 방식 — 요청 흐름

```
사용자 입력 (터미널)
    ↓
CLI 진입점 (openharness/cli.py)
    ↓
React TUI 서브프로세스 기동 (frontend/terminal/src/index.tsx)
    │
    ├── Python 백엔드 (ui/backend_host.py)
    │       ↓  stdin/stdout JSON 프로토콜 (OHJSON:)
    │
    └── React 프론트엔드 (App.tsx)
            │
            ├── 사용자 입력 처리 (useInput 훅)
            ├── 메시지 전송 → Python 백엔드
            └── 이벤트 수신 → UI 업데이트

Python 백엔드 내부 흐름:
    사용자 입력 수신
        ↓
    슬래시 커맨드 여부 확인 (commands/registry.py)
        ↓ (일반 텍스트면)
    QueryEngine.run() (engine/query_engine.py)
        ↓
    쿼리 루프 (engine/query.py)
        ├── LLM API 스트리밍 호출 (api/)
        ├── 도구 호출 파싱
        ├── PreToolUse 훅 실행 (hooks/)
        ├── 권한 체크 (permissions/)
        ├── 도구 실행 (tools/)
        ├── PostToolUse 훅 실행 (hooks/)
        └── 결과를 LLM에 다시 피드 → 반복
        ↓
    응답 이벤트 emit → React 프론트엔드
```

---

### 핵심 서브시스템 상세

#### `engine/` — 에이전트 루프

| 파일 | 용도 |
|------|------|
| `query_engine.py` | `QueryEngine` — 대화 히스토리 소유, LLM 클라이언트 관리, 비용 추적 |
| `query.py` | 핵심 스트리밍 루프 — 도구 호출 처리, 훅 실행, 컨텍스트 컴팩션 |
| `messages.py` | `ConversationMessage`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `ImageBlock` — 대화 구조 모델 |
| `stream_events.py` | `AssistantTextDelta`, `ToolExecutionStarted/Completed`, `AssistantTurnComplete` — UI에 전달되는 스트림 이벤트 |
| `cost_tracker.py` | 입력/출력 토큰 집계 및 비용 계산 |

---

#### `tools/` — 도구 구현 (44개+)

모든 도구는 `BaseTool`을 상속하고 `async execute()`를 구현.

| 카테고리 | 도구 |
|----------|------|
| **파일 I/O** | `FileRead`, `FileWrite`, `FileEdit`, `NotebookEdit` |
| **검색** | `Glob`, `Grep`, `ToolSearch` |
| **셸** | `Bash`, `Sleep` |
| **웹** | `WebFetch`, `WebSearch` |
| **워크스페이스** | `EnterWorktree`, `ExitWorktree`, `EnterPlanMode`, `ExitPlanMode` |
| **태스크/크론** | `TaskCreate/Get/List/Stop/Output/Update`, `CronCreate/List/Delete/Toggle` |
| **MCP** | `McpAuth`, `ListMcpResources`, `ReadMcpResource`, `McpToolAdapter` |
| **멀티에이전트** | `Agent` (서브에이전트 스포닝), `TeamCreate`, `TeamDelete` |
| **스킬/사용자** | `Skill` (스킬 로드), `AskUserQuestion`, `SendMessage` |
| **원격** | `RemoteTrigger`, `Lsp` |

`tools/base.py` 핵심 클래스:
- `BaseTool` — 추상 기반, `async execute()` 구현 필수
- `ToolRegistry` — 이름 → 인스턴스 맵, LLM API 스키마 생성
- `ToolExecutionContext` — 실행 중 공유 상태 (cwd, 메타데이터)
- `ToolResult` — 정규화 출력 (텍스트, 오류 여부, 메타데이터)

---

#### `api/` — LLM 프로바이더 클라이언트

`SupportsStreamingMessages` 프로토콜을 구현하는 클라이언트들.

| 파일 | 용도 |
|------|------|
| `client.py` | `ApiMessageRequest`, `ApiStreamEvent` — 공통 인터페이스 |
| `openai_client.py` | OpenAI-호환 클라이언트 (Ollama, DashScope, Hanplanet 등 포함) |
| `copilot_client.py` | GitHub Copilot 클라이언트 |
| `codex_client.py` | Codex CLI 브릿지 클라이언트 |
| `errors.py` | `AuthenticationFailure`, `RateLimitFailure`, `RequestFailure` |
| `usage.py` | `UsageSnapshot` — 토큰 사용량 스냅샷 |

---

#### `ui/` — TUI 백엔드 호스트

| 파일 | 용도 |
|------|------|
| `backend_host.py` | **핵심** — stdin/stdout JSON 프로토콜 서버, 모든 이벤트 emit, Hanplanet 커스텀 로직 |
| `protocol.py` | `BackendEvent`, `FrontendRequest`, `TranscriptItem` — 통신 스키마 |
| `runtime.py` | React TUI 서브프로세스 기동 및 라이프사이클 관리 |
| `textual_app.py` | Textual 기반 폴백 TUI (React 없을 때) |

**프로토콜:** Python 백엔드는 stdout에 `OHJSON:{...}\n` 형식으로 이벤트를 내보내고, React 프론트엔드는 stdin으로 JSON 요청을 전송.

주요 이벤트 타입:

| 이벤트 | 방향 | 의미 |
|--------|------|------|
| `ready` | B→F | 백엔드 초기화 완료 |
| `assistant_delta` | B→F | LLM 텍스트 스트리밍 |
| `assistant_complete` | B→F | LLM 응답 완료 |
| `tool_started` | B→F | 도구 실행 시작 |
| `tool_completed` | B→F | 도구 실행 완료 |
| `line_complete` | B→F | 턴 종료, 스피너 해제 |
| `select_request` | B→F | 선택 모달 요청 (`kind: "select"` 단일 / `kind: "multiselect"` 체크박스) |
| `modal_request` | B→F | 권한/질문 모달 요청 |
| `submit_line` | F→B | 사용자 입력 전송 |
| `apply_select_command` | F→B | 선택 모달 결과 전송 |
| `permission_response` | F→B | 권한 응답 |

---

#### `commands/` — 슬래시 커맨드 (registry.py)

54개+ 커맨드를 `SlashCommand` + 비동기 핸들러로 등록.

```python
# 패턴
async def _my_handler(args: str, context: CommandContext) -> CommandResult:
    return CommandResult(message="결과 텍스트")

registry.register(SlashCommand("my-cmd", "설명", _my_handler))
```

| 커맨드 그룹 | 예시 |
|-------------|------|
| 세션 관리 | `/clear`, `/rewind`, `/resume`, `/session` |
| 모델/프로바이더 | `/model`, `/provider`, `/effort`, `/passes`, `/fast` |
| 설정 | `/config`, `/theme`, `/permissions`, `/language` |
| 메모리/스킬 | `/memory`, `/skills` |
| 태스크/에이전트 | `/tasks`, `/agents` |
| 도구/컨텍스트 | `/context`, `/status`, `/cost`, `/stats` |

---

#### `permissions/` — 권한 체크

| 파일 | 용도 |
|------|------|
| `checker.py` | `PermissionChecker.evaluate()` — 모드+규칙 기반 판단 |
| `modes.py` | `DEFAULT`(확인), `AUTO`(자동 허용), `PLAN`(쓰기 차단) |

민감 경로 하드코딩 차단 목록: SSH 키, AWS 크리덴셜, kubeconfig 등.

---

#### `hooks/` — 라이프사이클 훅

| 파일 | 용도 |
|------|------|
| `executor.py` | `HookExecutor` — command/HTTP/prompt/agent 훅 실행 |
| `loader.py` | `HookRegistry` — `settings.json`에서 로드, 핫리로드 지원 |
| `events.py` | `PRE_TOOL_USE`, `POST_TOOL_USE`, `PRE_PROMPT`, `ON_SESSION_START` 등 |

---

#### `skills/` — 온디맨드 지식 주입

`.md` 파일 기반 스킬을 시스템 프롬프트에 주입. 커스텀 `preload_skills` 설정으로 로컬 모델에서도 사용 가능.

| 파일 | 용도 |
|------|------|
| `loader.py` | 번들/사용자/프로젝트 스킬 로드 |
| `registry.py` | `SkillRegistry` — 이름 → `SkillDefinition` 맵 |
| `types.py` | `SkillDefinition` — name, description, content |

스킬 검색 순서: 번들 내장 → `~/.openharness/skills/` → `.openharness/skills/`

---

#### `memory/` — 프로젝트 영속 메모리

| 파일 | 용도 |
|------|------|
| `manager.py` | `add/remove/list_memory_entry()` — 파일 단위 잠금 포함 |
| `search.py` | 내용 기반 검색 (메타데이터 가중치 우선) |
| `paths.py` | `~/.openharness/memory/<project>/` 경로 계산 |

---

#### `config/` — 설정

| 파일 | 용도 |
|------|------|
| `settings.py` | `Settings` (880줄+ Pydantic) — 모든 런타임 설정의 단일 진실 소스 |
| `paths.py` | `~/.openharness/`, `.openharness/` 경로 해결 |

설정 해결 우선순위: CLI 인자 > 환경변수 > `settings.json` > 기본값

---

#### `swarm/` + `coordinator/` — 멀티에이전트

| 파일 | 용도 |
|------|------|
| `swarm/types.py` | `BackendType` — subprocess / in_process / tmux / iTerm2 |
| `swarm/team_lifecycle.py` | 팀 생성, 코디네이션, 해제 |
| `swarm/mailbox.py` | 에이전트 간 메시지 전달 |
| `coordinator/coordinator_mode.py` | `TeamRegistry` — 팀 및 에이전트 상태 추적 |

---

### 프론트엔드 구조 (frontend/terminal/src/)

#### 주요 컴포넌트

| 컴포넌트 | 용도 |
|----------|------|
| `App.tsx` | 최상위 — 입력 처리, 모달 관리, 커맨드 피커 |
| `ConversationView.tsx` | 대화 히스토리 스크롤 뷰 |
| `PromptInput.tsx` | 사용자 입력창 + 스피너 |
| `Spinner.tsx` | `ink-spinner` 기반 로딩 인디케이터, 커스텀 VERBS |
| `CommandPicker.tsx` | `/` 입력 시 나타나는 커맨드 선택 드롭다운 |
| `SelectModal.tsx` | ↑↓ 단일 선택 모달 (모델, 프로바이더, 세션 등) |
| `MultiSelectModal.tsx` | 체크박스 복수 선택 모달 (프로바이더 등록 시 인증 방식 선택) |
| `ModalHost.tsx` | 권한 확인, 질문 모달 |
| `StatusBar.tsx` | 하단 상태바 (모델, 모드, 토큰, 태스크 수) |
| `TodoPanel.tsx` | 투두 목록 표시, Ctrl+T 토글 |
| `SwarmPanel.tsx` | 멀티에이전트 팀 상태, Ctrl+W 토글 |
| `ToolCallDisplay.tsx` | 도구 호출/결과 인라인 표시 |
| `MarkdownText.tsx` | 마크다운 → ANSI 렌더링 |
| `OAuthCountdown.tsx` | OAuth 타임아웃 카운트다운 |
| `WelcomeBanner.tsx` | HANPLANET ASCII 배너 |

#### `hooks/useBackendSession.ts`

React ↔ Python 통신의 핵심.

- Python 백엔드 서브프로세스 기동 (`spawn`)
- stdout를 readline으로 읽어 `OHJSON:` 접두사 파싱
- 이벤트별 상태 업데이트 (`transcript`, `tasks`, `status`, `modal` 등)
- 스트리밍 텍스트 50ms 디바운스 플러시 (토큰 단위 리렌더 방지)
- `sendRequest(payload)` — stdin으로 JSON 전송

#### `types.ts`

| 타입 | 용도 |
|------|------|
| `FrontendConfig` | 백엔드 커맨드, 초기 프롬프트 |
| `TranscriptItem` | role / text / tool_name / tool_input / is_error |
| `TaskSnapshot` | id / type / status / description / metadata |
| `BackendEvent` | 백엔드 → 프론트엔드 이벤트 유니온 |
| `SelectOptionPayload` | value / label / description / active |
| `McpServerSnapshot` | MCP 서버 상태 |
| `SwarmTeammateSnapshot` | 에이전트 팀원 상태 |

---

### ohmo/ — 개인 에이전트 CLI

`oh`/`hanplanet`과 독립된 별도 CLI. 채팅앱 채널에서 항상 작동하는 개인 에이전트를 구성.

| 파일 | 용도 |
|------|------|
| `cli.py` | `ohmo` 진입점 — `init`, `config`, `gateway start/stop/status` |
| `workspace.py` | `~/.ohmo/` 워크스페이스 (`soul.md`, `user.md`, `state/`, `memory/`) |
| `runtime.py` | TUI/백그라운드 세션 실행 |
| `gateway/service.py` | `OhmoGatewayService` — 멀티채널 게이트웨이 오케스트레이터 |
| `gateway/models.py` | `GatewayConfig`, `GatewayState` |

지원 채널: Slack, Discord, Telegram, WhatsApp, Email, Matrix, Feishu, DingTalk, QQ

---

### 진입점 (pyproject.toml)

```toml
[project.scripts]
hanplanet = "openharness.cli:app"   # 커스텀 진입점
oh        = "openharness.cli:app"   # 원본 alias
openh     = "openharness.cli:app"   # Windows용
ohmo      = "ohmo.cli:app"          # 개인 에이전트
```

---

### 핵심 추상화 요약

| 추상화 | 위치 | 역할 |
|--------|------|------|
| `QueryEngine` | `engine/query_engine.py` | 대화 히스토리 + LLM 클라이언트 오케스트레이터 |
| `BaseTool` | `tools/base.py` | 모든 도구의 기반 클래스 |
| `ToolRegistry` | `tools/base.py` | 도구 이름 → 인스턴스 맵 |
| `PermissionChecker` | `permissions/checker.py` | 도구 실행 전 접근 제어 판단 |
| `HookExecutor` | `hooks/executor.py` | 라이프사이클 이벤트 실행 |
| `SkillRegistry` | `skills/registry.py` | 온디맨드 지식 주입 |
| `CommandRegistry` | `commands/registry.py` | 슬래시 커맨드 디스패치 |
| `BackendEvent` | `ui/protocol.py` | 프론트↔백 통신 이벤트 스키마 |
| `Settings` | `config/settings.py` | 모든 런타임 설정의 단일 진실 소스 |
