# 디스코드 AI 자동 제재 봇

서버 규칙을 기준으로 AI(Gemini 1차 / Groq 폴백)가 메시지를 판단하여 경고 → 삭제 → 타임아웃 → 킥 → 밴까지
단계적으로 자동 제재하는 discord.py 기반 봇입니다. 무료 API 티어로 운영 가능하도록 설계했습니다.

## 구조

```text
discord-automod/
├── bot.py          # 메인 봇 (이벤트, 명령어, 제재 실행, 폴백 조치 제한, 배치 감사 스케줄링)
├── batch_audit.py    # 배치 감사 핵심 로직 + 독립 실행 스크립트 (조치 없이 리포트만 생성)
├── moderator.py     # Gemini(1차) + Groq(폴백) API로 메시지 위반 여부 판단, 배치 판단 지원
├── filters.py        # 금칙어/스팸/초대링크 1차 필터 (AI 호출 없이 즉시 처리)
├── cache.py           # 동일/반복 메시지 판단 결과 캐싱
├── database.py         # SQLite로 유저별 누적 위반 점수 + 판단 로그 + 채널 체크포인트 관리
├── learning.py          # 관리자 오탐 확정 학습, 채널 범위 규칙, 예시 정제와 취소 처리
├── config.py             # 서버 규칙, 제재 단계, 모델/한도, 배치 감사 설정 (여기를 주로 수정)
├── run_bot.bat           # Windows 실행기 (환경/DB 점검, 재시작 횟수 제한)
├── register_startup.bat  # Windows 시작프로그램 바로가기 등록
├── 봇실행.bat             # run_bot.bat을 호출하는 한국어 바로가기
├── 시작프로그램_등록.bat   # register_startup.bat을 호출하는 한국어 바로가기
├── tests/                # 필터·DB·배치 실패 처리 회귀 테스트
├── requirements.txt
└── .env.example
```

초기값은 `MANUAL_REVIEW_MODE=True`입니다. 판단 결과만 관리자 카드로 올리고 실제 제재는
관리자가 버튼으로 확정합니다. 충분히 검증한 뒤에만 자동 모드로 전환하세요.

## 동작 원리

1. 메시지가 올라오면 `filters.py`가 먼저 금칙어/초대링크/도배 여부를 정규식으로 즉시 판별합니다.
   여기서 결론이 나면 AI 호출 없이 바로 처리됩니다 (비용 절감).
2. 애매한 메시지만 `moderator.py`로 넘어가 **Gemini**에게 먼저 판단을 요청합니다.
   Gemini가 실패하거나(오류/한도초과) 응답을 못 주면 자동으로 **Groq**로 폴백합니다.
3. 등급별로 점수를 부여하고(`config.VIOLATION_LEVEL_POINTS`), 유저의 **누적 점수**를 SQLite에 기록합니다.
4. 누적 점수가 `config.STRIKE_THRESHOLDS`의 임계값을 넘으면 해당 조치를 실행합니다.
   - 1점~: 경고 / 3점~: 삭제+경고 / 6점~: 1시간 타임아웃 / 12점~: 24시간 타임아웃 / 20점~: 킥 / 30점~: 밴
   (모두 `config.py`에서 자유롭게 숫자 조정 가능)
5. **킥/밴은 절대 자동 실행되지 않습니다.** 실제 커뮤니티 정책(경고 2회 → 운영진이 최종 판단,
   2일 이내 이의제기 절차 존재)에 맞춰, 판단 주체가 필터든 Gemini든 Groq든 상관없이 킥/밴이
   나와야 하는 상황이면 자동으로 `config.AUTO_ACTION_CEILING`(기본: 24시간 타임아웃)으로
   하향되고, 로그 채널에 "⚠️ 관리자 검토 필요"로 강조 표시됩니다. `!BB 검토대기` 명령어로
   이런 건들만 모아 볼 수 있고, 실제 킥/밴은 관리자가 직접 결정해서 수동으로 처리합니다.
6. `EXTREME` 등급(노골적 위협, 음란물 등)은 누적 점수와 무관하게 즉시 강한 조치를 취하도록
   설정할 수 있습니다 (`IMMEDIATE_ACTION_FOR_EXTREME`) — 단, 이때도 킥/밴이면 위 5번 규칙이 그대로 적용됩니다.
7. 관리자(Administrator) 권한을 가진 유저만 자동 제재 대상에서 제외됩니다.
   메시지 관리 권한만 가진 모더레이터는 일반 유저와 동일하게 검사받습니다.
8. 모든 제재는 로그 채널에 임베드로 기록되며 **어느 모델(Gemini/Groq/필터)이 판단했는지**가 항상 남습니다.
   대상 유저에게는 DM으로 사유가 안내됩니다.
9. **익명화된 제재 로그 공개** (선택): `.env`의 `PUBLIC_LOG_CHANNEL_ID`를 설정하면 MODERATE 이상의
   제재가 발생할 때 공개 채널에 익명 로그가 게시됩니다. 커뮤니티 공지의 "제재 로그 공개 기능" 방침에 맞춰
   유저를 식별할 수 있는 정보(멘션/닉네임/ID/메시지 원문/AI 사유 문장)는 일절 포함하지 않고,
   위반 등급·위반 유형(규정 카테고리)·조치만 게시합니다. 최소 공개 등급은
   `config.PUBLIC_LOG_MIN_LEVEL`로 조정할 수 있습니다.

## 설치

Windows에서는 OneDrive가 가상환경 파일을 잠글 수 있으므로 동기화 폴더 밖의 전용 환경을 권장합니다.
아래 전용 Python 3.13 환경을 사용하며, 프로젝트 상위 폴더의 `venv`는 사용하지 않습니다.

```bat
cd discord-automod
uv python install 3.13
uv venv --python 3.13 "%LOCALAPPDATA%\DiscordAutoMod\venv-3.13"
uv pip install --python "%LOCALAPPDATA%\DiscordAutoMod\venv-3.13\Scripts\python.exe" -r requirements.lock
```

Linux/macOS:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

## 설정

1. `.env.example`을 `.env`로 복사 후 값 채우기:

   ```bash
   cp .env.example .env
   ```

   - `DISCORD_BOT_TOKEN`: [Discord Developer Portal](https://discord.com/developers/applications)에서
     봇 생성 후 발급. **Privileged Gateway Intents**에서 `MESSAGE CONTENT INTENT`와
     `SERVER MEMBERS INTENT`를 반드시 켜야 합니다.
   - `GEMINI_API_KEY`: [Google AI Studio](https://aistudio.google.com/apikey)에서 무료 발급 (카드 불필요).
   - `GROQ_API_KEY`: [Groq Console](https://console.groq.com/keys)에서 무료 발급 (카드 불필요).
   - `LOG_CHANNEL_ID`: 제재 로그를 남길 채널의 ID (채널 우클릭 → ID 복사, 개발자 모드 필요).
   - `AUTOMOD_DB_PATH`(선택): SQLite 파일 경로. OneDrive/Dropbox 같은 동기화 폴더 밖의
     로컬 경로를 권장합니다. 비워두면 프로젝트 폴더의 `automod.db`를 사용합니다.
   - `WATCHED_CHANNEL_IDS`(선택): 배치 감사 대상 채널 ID를 쉼표로 구분합니다.
     `.env`에 이 키가 있으면 `config.py`의 기본 목록보다 우선하며, 빈 값이면 배치 감사를 비활성화합니다.

2. `config.py`의 `SERVER_RULES`를 실제 서버 규칙으로 수정하세요. AI는 이 텍스트를
   그대로 판단 기준으로 사용합니다. 규칙을 구체적으로 쓸수록 판단 정확도가 올라갑니다.

3. 봇을 서버에 초대할 때 필요한 권한:
   - 메시지 관리 (Manage Messages)
   - 멤버 타임아웃 (Moderate Members)
   - 멤버 추방 (Kick Members)
   - 멤버 차단 (Ban Members)
   - 채널 보기 / 메시지 보내기

   최소 권한 운영 시에는 위 권한만 개별 부여할 수 있습니다. 현재 운영 정책은
   `config.ALLOW_ADMINISTRATOR_PERMISSION=True`로 Administrator 권한 유지를 명시적으로 허용합니다.
   이를 `False`로 바꾸면 로그인 시 관리자 권한을 경고합니다. 필수 권한 누락 검사는 설정과 무관하게 유지됩니다.

## 실행

- Windows: `run_bot.bat`을 더블클릭합니다.
- 설치 및 DB 설정 점검: `run_bot.bat --check` (한글/공백이 포함된 경로도 지원)
  이 점검은 Python 패키지, SQLite 접근, 필수 토큰/API 키와 채널 ID 형식을 확인하며 Discord에는 로그인하지 않습니다.
- 읽기 전용 외부 연결 점검: `run_bot.bat --check-network`
  Discord 봇 토큰·설정 채널 조회와 Gemini/Groq 모델 조회 API를 확인하며
  메시지 생성·전송이나 제재는 수행하지 않습니다.
- 자동 시작 등록: `register_startup.bat`을 한 번 실행합니다.
- 자동 시작 상태만 확인: `register_startup.bat --check` (등록하거나 변경하지 않음)
- Linux/macOS: 활성화한 가상환경에서 `python bot.py`를 실행합니다.

## 명령어 (전부 관리자 전용 — 서버 내에서 `!BB` 접두사 사용, `!bb` 소문자·`!BB점수` 붙여쓰기 모두 인식)

| 명령어 | 권한 | 설명 |
| --- | --- | --- |
| `!BB 명령어` | 관리자 권한 | 전체 명령어 목록 확인 (별칭: `도움말`, `help`) |
| `!BB 점수 @유저` | 관리자 권한 | 해당 유저의 누적 위반 점수 및 최근 이력(판단 모델 포함) 확인 |
| `!BB 점수초기화 @유저` | 관리자 권한 | 유저의 누적 점수 초기화 |
| `!BB 규칙확인` | 관리자 권한 | 현재 설정된 서버 규칙 확인 |
| `!BB 검토대기 [시간]` | 관리자 권한 | 킥/밴이 자동 실행 대신 하향 조정된 건 목록 (기본 최근 72시간) |
| `!BB 오탐학습 [개수]` | 관리자 권한 | 활성 오탐 학습 규칙과 적용 범위 조회 |
| `!BB 오탐취소 <규칙번호>` | 관리자 권한 | 잘못 등록한 오탐 학습 규칙 비활성화 |
| `!BB 상태` | 관리자 권한 | 처리 대기열/워커/드롭 건수 확인 |
| `!BB 감사실행 [backend]` | 관리자 권한 | 배치 감사를 지금 바로 실행 (backend 생략 시 `config.BATCH_BACKEND` 사용) |

검수 카드(#제재-로그)의 버튼도 명령어와 마찬가지로 **모두 관리자 전용**입니다.
`정상 · 이 채널 학습`은 해당 채널에서만 동일 오탐을 허용합니다. 스레드에서는
부모 채널 범위로 적용됩니다. `정상 · 서버 전체 학습`은 모든 채널에 적용되므로,
어디서나 정상인 문구가 확실할 때만 사용하세요. 기존 `false_positive` 검수 이력은
첫 시작 시 채널 범위 규칙으로 자동 이전됩니다.

## Gemini + Groq 이중화 & 무료 티어 참고사항

- **왜 이중화하나**: Gemini 무료 티어(대략 Flash 기준 하루 1,500회 안팎, 시기에 따라 변동)만 쓰면
  트래픽이 늘 때 한도 소진으로 판단이 아예 안 되는 상황이 생길 수 있습니다. Groq(별도의 하루 한도)를
  폴백으로 두면 한쪽이 막혀도 서비스가 끊기지 않습니다.
- **모델이 다르면 판단도 달라질 수 있음**: Gemini와 Groq(오픈소스 모델)는 학습 데이터와 안전 기준이 달라
  같은 메시지에 다른 등급을 매길 수 있습니다. 다만 이 프로젝트는 판단 주체와 무관하게 **킥/밴을
  아예 자동 실행하지 않는 정책**(`config.AUTO_ACTION_CEILING`)을 쓰므로, 모델 간 판단 편차로 인한
  리스크는 애초에 킥/밴 단계에서 차단됩니다. 경고/삭제/타임아웃까지는 모델 종류와 무관하게 자동 실행됩니다.
- **무료 티어 데이터 정책 주의**: Gemini 무료 티어는 약관상 입력/출력이 모델 개선에 활용될 수 있습니다.
  민감한 대화가 오가는 서버라면 이 점을 고려하세요.
- 필요하면 `moderator.py`에 다른 무료 API(Cerebras, OpenRouter 등)를 3차 폴백으로 추가하는 것도
  동일한 패턴(`_classify_with_xxx` 함수 추가 후 `classify_message`의 예외 체인에 연결)으로 확장 가능합니다.

## 대규모 서버(수천~수만 유저) 운영 가이드

1. **1차 필터 (`filters.py`)**: 금칙어(`BANNED_WORDS_SEVERE`/`MODERATE`), 초대 링크, 도배를 정규식으로
   즉시 판별합니다. 여기서 결론이 나면 AI 호출 없이 바로 처리되어 비용이 들지 않습니다.
2. **캐시 (`cache.py`)**: 동일/유사 문구가 반복되면(레이드성 스팸 등) 이전 판단 결과(및 어느 모델이 판단했는지)를
   재사용합니다. 기본 10분(`CACHE_TTL_SECONDS`) 캐시.
3. **큐 + 워커 풀 (`bot.py`)**: 애매한 메시지만 큐에 들어가고, `MAX_CONCURRENT_AI_CALLS`(기본 8)개의
   워커가 동시에 AI를 호출합니다. `on_message`는 절대 AI 응답을 기다리지 않습니다.
4. **과부하 보호**: 큐가 `MAX_QUEUE_SIZE`(기본 5000)를 넘거나 `MAX_QUEUE_AGE_SECONDS`(기본 120초)를
   넘겨 대기한 메시지는 검사하지 않고 버립니다. 이 누락이 `DROP_ALERT_THRESHOLD`(기본 50건)마다
   로그 채널에 경고로 올라오고, `!BB 상태`에서 대기열 사용률과 누락 건수를 확인할 수 있습니다.
5. **SQLite WAL 모드**: DB 쓰기는 실제 위반 감지 시에만 발생하므로 SQLite로도 상당한 트래픽을 감당합니다.
   여러 인스턴스로 수평 확장해야 한다면 PostgreSQL(asyncpg) 이전을 고려하세요.

### 튜닝 팁

- 금칙어 목록을 채울수록 AI 호출 비중이 줄어 무료 한도 내에서 더 오래 버틸 수 있습니다.
- `MAX_CONCURRENT_AI_CALLS`를 무작정 높이면 Gemini/Groq 양쪽 레이트리밋에 걸릴 수 있으니
  각 제공자의 분당 요청 제한을 확인 후 설정하세요.
- `!BB 검토대기`를 주기적으로 확인해 폴백이 얼마나 자주 발동하는지 파악하고,
  너무 잦다면 Gemini 한도 자체를 유료로 올리는 것도 고려해보세요.

## 배치 감사(Batch Audit) — 실시간과 별개의 "정리 리포트" 모드

실시간 자동제재와 달리 **조치를 자동 실행하지 않고**, 등록한 채널들의 대화를 주기적으로 모아
운영 규정 기준으로 분류·태깅한 뒤 유저별로 정리된 리포트만 만들어줍니다. 최종 판단은 관리자가 합니다.

### 배치 감사 설정

1. `.env`의 `WATCHED_CHANNEL_IDS`에 감시할 채널 ID를 쉼표로 구분해 등록합니다.
2. `BATCH_BACKEND`로 기본 판단 백엔드를 고릅니다: `"auto"`(Gemini→Groq 폴백, 기본) / `"gemini"` / `"groq"` / `"ollama"`.
3. 로컬 Ollama를 쓰려면 [Ollama](https://ollama.com)를 설치하고 `ollama pull qwen2.5:14b`(또는 원하는 모델)로
   받은 뒤, `config.py`의 `OLLAMA_MODEL`을 맞춰주세요. 한국어 뉘앙스 판단이 중요하므로 한국어 성능이
   검증된 모델(Qwen, EXAONE 계열 등)을 권장하며, VRAM에 맞춰 크기를 조정하세요.
4. `.env`의 `REPORT_CHANNEL_ID`를 설정하면 리포트가 디스코드에도 자동 전송됩니다 (비워두면 로컬 파일만 저장).

### 실행 방식 (둘 다 지원, 동시에 써도 됨)

#### 1) 통합 실행 — 지금 봇과 함께 24/7 서버에서

매일 `config.BATCH_RUN_HOUR_KST`시(기본 새벽 4시, 한국 시간)에 봇이 자동으로 감사를 돌립니다.
봇을 재시작해도 그 즉시 실행되지 않아 API 한도를 아낍니다. 별도 설정 없이
`WATCHED_CHANNEL_IDS`만 채워두면 `bot.py` 실행 시 자동으로 예약됩니다.
필요하면 `!BB 감사실행 [gemini|groq|ollama|auto]` 명령어로 즉시 수동 실행도 가능합니다
(관리자 권한 필요).

#### 2) 독립 실행 — 개인 PC에서 주 1회, 로컬 GPU + Ollama로

```bash
python batch_audit.py --backend ollama
```

이 스크립트는 디스코드에 한 번 접속해서 감사를 끝내고 바로 종료되므로, 상시 실행 중인 봇과는
별개로 운영할 수 있습니다. Windows 작업 스케줄러(주 1회, 컴퓨터를 안 쓰는 새벽 시간대 등)나
macOS/Linux의 cron에 등록해두면 됩니다.

예) Linux/macOS cron으로 매주 일요일 새벽 4시 실행:

```bash
0 4 * * 0 cd /path/to/discord-automod && /path/to/venv/bin/python batch_audit.py --backend ollama >> audit.log 2>&1
```

### 동작 방식 & 비용 최적화

- 채널별로 **마지막으로 처리한 메시지 지점(체크포인트)**을 SQLite에 저장해, 다음 실행 때는
  그 이후 메시지만 가져옵니다 (중복 검토 없음).
- **첫 실행은 최근 `BATCH_FIRST_RUN_LOOKBACK_DAYS`(기본 7일)만 수집**합니다. 채널 전체 이력을
  긁으면 대형 서버에서 수십만 건이 되어 API 한도에 걸릴 수 있기 때문입니다. 또한 실행 1회당
  채널별 `BATCH_MAX_MESSAGES_PER_CHANNEL`(기본 3,000건) 상한이 있으며, 상한에 걸린 나머지는
  다음 실행에서 자동으로 이어서 처리됩니다.
- 메시지를 `config.BATCH_SIZE`(기본 25개)개씩 묶어서 **한 번의 LLM 호출로 동시에 판단**합니다.
  실시간처럼 메시지 1건당 1회 호출하지 않으므로 토큰/요청 수가 훨씬 절약됩니다.
- 리포트는 `config.REPORT_OUTPUT_DIR`(기본 `./reports`)에 Markdown 파일로 저장되고,
  유저별로 위반 등급·규정 번호·판단 모델·원문 스니펫·메시지 링크가 정리되어 있습니다.
- 배치 감사는 **자동으로 삭제/타임아웃/킥/밴을 실행하지 않습니다.** 실시간 자동제재(`bot.py`의
  `on_message`)와 완전히 분리된 기능이라, 여기서 나온 리포트를 보고 관리자가 수동으로 조치합니다.

## 주의사항

- AI 판단은 100% 정확하지 않으므로, 처음에는 `STRIKE_THRESHOLDS`를 관대하게 설정하고
  로그 채널을 지켜보며 튜닝하는 것을 권장합니다.
- 오탐(false positive) 방지를 위해 Gemini/Groq 둘 다 실패하면 안전하게 `NONE`(위반 없음)으로 처리합니다.
  이 상태가 `AI_OUTAGE_ALERT_THRESHOLD`회 연속되면 서버별로 장애 경고가 로그 채널에 올라옵니다.
- 자동 조치 모드에서 **실제로 집행에 성공한 제재만 누적 점수에 반영**됩니다. 권한 부족 등으로
  조치가 실패하면 점수를 올리지 않아, 제재받지 않은 유저가 다음 위반에서 과잉 처벌되는 일을 막습니다.
- 관리자 검수 처리 도중 봇이 비정상 종료된 건은 자동 재시도하지 않습니다. `!BB 검토대기`에
  중단 의심 건이 표시되면 Discord 감사 로그와 대상 상태를 확인하고, 제재가 적용되지 않은 것이
  확실한 건만 `!BB 검토복구 <번호>`로 다시 대기 상태로 전환하세요.
- `STRIKE_DECAY_DAYS`(기본 30일)가 지나면 점수가 자동으로 절반 감소합니다.
- 개인정보 보호를 위해 `config.VIOLATION_CONTENT_RETENTION_DAYS`의 기본값은 90일입니다.
  기간이 지난 완료 기록의 메시지 원문만 비우며, `0`으로 설정하면 자동 익명화를 비활성화합니다.
  아직 검토 중인 기록과 오탐 학습 데이터는 자동 익명화 대상에서 제외됩니다.
- `config.REPORT_RETENTION_DAYS`의 기본값은 180일입니다. `REPORT_OUTPUT_DIR` 안의 오래된
  `audit_report_*.md`만 정리하며 백업과 다른 파일은 삭제하지 않습니다. `0`이면 비활성화됩니다.
