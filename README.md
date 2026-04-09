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
uv sync --extra dev

# TUI 프론트엔드 의존성 설치
cd frontend/terminal && npm ci && cd ../..
```

### 전역 설치 (pipx)

```bash
pipx install -e .
```

설치 후 `oh` 명령어로 실행.

> **개발 중 소스 코드 직접 참조 (pipx 환경)**
>
> pipx venv에서 소스 코드 변경이 즉시 반영되려면 `.pth` 파일을 추가한다.
>
> ```bash
> # 경로 예시 (Python 버전에 맞게 수정)
> echo "/path/to/HanHarness
> /path/to/HanHarness/src" \
>   > ~/.local/pipx/venvs/openharness-ai/lib/python3.13/site-packages/_openharness_ai.pth
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

### Provider 목록 커스텀

**파일:** `src/openharness/ui/backend_host.py`

- `/provider` 목록에서 `ollama`, `hanplanet` 외 프로바이더 숨김 (삭제 아님)
- Hanplanet 설명문: `"Hanplanet / Hanplanet oauth, key"`
- Hanplanet 선택 시 항상 인증 방법 선택 화면 표시 (계정 전환 지원)
- 프로바이더 선택 완료 후 자동으로 모델 선택 화면 이동

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

---

### Ollama 로컬 프로바이더 지원

- 기본 base URL에 `/v1` suffix 적용
- API 키 없이 로컬 Ollama 서버 연결 지원
- 로컬/원격 Ollama 모델 목록 분리 표시

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
git remote add upstream https://github.com/HKUDS/OpenHarness.git  # 최초 1회
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

`backend_host.py`는 원본 로직도 포함되어 있으므로 파일 전체를 `--ours`로 처리하지 말고 **충돌 구간을 직접 확인하며 병합**한다.

#### 업스트림 버전(theirs)을 우선해야 하는 파일

아래 영역은 upstream 업데이트를 적극 수용한다.

- `src/openharness/commands/registry.py` — 슬래시 커맨드 로직
- `src/openharness/engine/` — 모델 작동 로직, 최적화
- `src/openharness/api/` — API 클라이언트
- `src/openharness/tools/` — 툴 구현
- `src/openharness/config/settings.py` — 설정 스키마
- 그 외 나머지 파일 전반

충돌 해결 명령어 예시 (파일 전체를 upstream 버전으로):

```bash
git checkout --theirs src/openharness/commands/registry.py
git add src/openharness/commands/registry.py
```

### 병합 완료 후 확인

```bash
# 커스텀 내용이 남아있는지 확인
grep -n "HanHarness" src/openharness/prompts/system_prompt.py
grep -n "hanplanet" pyproject.toml
grep -n "_hanplanet_oauth_flow\|HANPLANET\|_VISIBLE_PROFILES" src/openharness/ui/backend_host.py
```
