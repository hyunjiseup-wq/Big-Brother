"""
서버 규칙 및 자동 제재 설정 파일.
- SERVER_RULES: AI가 판단 기준으로 삼을 서버 규칙 (관리자가 자유롭게 수정)
- STRIKE_THRESHOLDS: 누적 위반 점수(strike)에 따른 제재 단계
- VIOLATION_LEVEL_POINTS: AI가 판단한 위반 등급별 부여 점수
"""
import os

from dotenv import load_dotenv


load_dotenv()


def _parse_channel_id_list(raw: str) -> list[int | str]:
    """쉼표로 구분한 채널 ID를 파싱하며 오류 값은 시작 검증이 설명하도록 보존한다."""
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            values.append(item)
    return values

# ── 서버 규칙 (Escape from Tarkov 한국 커뮤니티 실제 약관 기반) ────────────
# 원문 중 "음성 채팅방 이용 규정(B)"과 "인게임 매너(Ⅲ)"는 텍스트 채팅만 읽는 이 봇으로는
# 판별할 수 없어 제외했습니다. 텍스트로 감지 가능한 커뮤니티 약관(A)만 반영했습니다.
SERVER_RULES = """
[커뮤니티 채팅 규정 - 위반 시 경고(2회) 후 제재]

1. 디스코드 가이드라인 및 BSG(Escape from Tarkov) 이용약관 위반
   - 디스코드 자체 정책(혐오발언, 괴롭힘, 성적 콘텐츠, 폭력 조장 등)을 위반하는 내용

2. 커뮤니티 무단 홍보
   - 허가받지 않은 서버 초대 링크, 외부 사이트/채널 홍보, 광고성 게시

3. 채팅 예절 미준수
   - 반말 (서버는 존댓말 사용을 기본 예절로 함)
     ※ 반말 판단 시 주의: 존댓말 어미를 장난스럽게/귀엽게 변형한 표현은 존댓말로 취급하며 위반이 아님.
       예: "~하세용", "~하나용", "알겠어용" 등 어미에 ㅇ을 붙인 용용체,
           "알겠습니당", "감사합니당" 등 "~습니다"를 "~습니당"으로 변형한 것,
           "넵", "넹", "옙" 같은 "네"의 변형.
       기본 어미가 존댓말(요/니다/네)이면 변형이 있어도 반말이 아님.
   - 과도한 친목 행위 (특정 유저들끼리만 어울리며 배타적인 분위기 조성)
   - 정치적 발언 (정치인, 정당, 정치 이슈에 대한 논쟁적 발언)
   - 현금 거래 유도 (게임 아이템/계정 등의 현금 거래 시도)
     ※ 현금 거래 판단 시 주의: "현금 거래"란 현실 재화(현금, 계좌이체, 문화상품권, 페이팔 등)로
       게임 아이템/계정을 사고파는 행위(RMT)만을 뜻함. 게임 내 화폐(루블/달러/유로)로 하는
       아이템 시세·판매·구매 대화는 이 게임의 정상적인 콘텐츠이며 절대 위반이 아님.
       한국 유저들은 게임 내 루블·달러·유로 금액을 관용적으로 모두 "원"이라 부르기도 함
       (예: "테라피스트한테 팔면 18만원 나와요" = 게임 내 상인에게 루블로 판매한다는 뜻, 위반 아님).
       "원"이라는 단어만으로 현금 거래로 단정하지 말고, 현실 결제 수단 언급이나
       디스코드 밖 연락 유도(카톡, 오픈채팅 등) 같은 명확한 정황이 있을 때만 위반으로 판단할 것.

4. 건전한 분위기를 해치거나 불필요한 갈등·불편을 유발하는 행위
   - 타인을 자극하는 시비, 조롱, 분란 조장, 분탕 목적의 발언
   - 특정 유저에 대한 지속적인 괴롭힘, 위협, 신상 공개
   - 근거 없는 핵/불공정 플레이 의혹으로 특정 유저를 공개적으로 저격·비방하는 행위
     (의혹 제보는 지정된 신고 채널을 통해서만 해야 하며, 공개 채팅에서의 저격은 갈등 유발로 판단)

5. 핵(치트) 관련 행위
   - 핵 사용 조장, 핵 판매/구매 유도, 핵 사용 자랑
   - 핵 사용자와의 의도적 동반 플레이(핵 버스)를 모집하거나 권유하는 발언

6. 위 항목에 명시되지 않았더라도 위와 유사한 성격의 위반 행위

[제재 원칙]
- 위 사항은 정도에 따라 경고(2회 누적 시 제재) ~ 즉시 제재까지 차등 적용됩니다.
- 명백하고 심각한 위반(혐오발언, 신상 공개, 성적/폭력적 콘텐츠, 위협, 핵 사용 조장 등)은 경고 단계 없이 바로 강한 조치가 필요할 수 있습니다.
- 경미한 예절 위반(가벼운 반말, 사소한 친목 발언 등)은 낮은 등급으로 판단하세요.
- [증거 기준] 이 커뮤니티는 "합리적인 의심이 없는 정도의 증명"을 제재 기준으로 삼습니다.
  메시지 내용이 단순 의혹 제기·정황 언급 수준인지, 명백한 규정 위반 발언인지 구분하고,
  애매하면 낮은 등급으로 보수적으로 판단하세요. 다만 "의혹 제기" 형식을 빌린
  특정 유저 공개 저격·조리돌림은 4번 위반으로 판단합니다.
"""

# ── 채널별 특수 규칙 (채널 ID 또는 채널 이름 -> AI에게 추가로 전달할 규칙/맥락) ──
# 특정 채널에서만 다르게 적용해야 하는 규칙을 여기에 적는다.
# 실시간 검사와 배치 감사 모두, 해당 채널의 메시지를 판단할 때 이 내용을
# 서버 규칙과 함께 AI에게 전달한다 (포럼 글/스레드는 부모 채널의 규칙을 상속).
# 키는 채널 ID(숫자) 또는 채널 이름(문자열) 둘 다 가능. 이름 매칭은
# '-'/'_'/공백/대소문자 차이를 무시한다 (예: "핵의심신고" ↔ #핵-의심-신고).
# 채널 이름이 바뀌면 이름 키는 더 이상 매칭되지 않으므로, ID를 알면 ID 등록을 권장.
CHANNEL_CONTEXT_NOTES = {
    # 물물교환 채널: 게임 내 화폐로 하는 아이템 거래 전용 채널
    1526179570192093314: (
        "이 채널은 게임 아이템을 게임 내 화폐(루블/달러/유로)로 사고팔거나 맞교환하는 "
        "물물교환 전용 채널입니다. 아이템 판매/구매/교환 글과 가격 표기는 이 채널의 "
        "정상적인 용도이므로 위반이 아닙니다. 한국 플레이어들은 유로든 루블이든 게임 내 "
        "화폐 금액을 통상적으로 모두 '원'으로 표기하므로, '원' 표기만으로 현금 거래(RMT)로 "
        "판단하지 마세요.\n"
        "이 채널에서는 아래의 명확한 정황이 있을 때만 위반으로 판단합니다:\n"
        "- 개인 DM이나 디스코드 밖 연락처(카톡, 오픈채팅 등)로 거래를 유도하는 경우\n"
        "- 계좌번호 등 현실 결제 수단(계좌이체, 문화상품권, 페이팔 등)이 올라오는 경우"
    ),
    # 핵 의심 신고 채널: 서버가 지정한 제보 채널이므로 의심 유저 지목 글이 정상
    1445049743150415923: (
        "이 채널은 서버가 지정한 핵(치트) 의심 유저 신고 전용 채널입니다. 본인이 느끼기에 "
        "의심스러운 유저의 닉네임과 전적(오버롤) 스크린샷, 킬캠/클립 등을 올려 제보하는 것이 "
        "이 채널의 정상적인 용도입니다. 따라서 특정 유저를 지목하며 핵 의혹을 제기하는 글은 "
        "규칙 4의 '공개 저격'이 아니라 규칙 4가 허용한 '지정된 신고 채널을 통한 의혹 제보'이므로 "
        "위반이 아닙니다. 확실한 증거 없이 개인적인 의심만으로 올린 제보도 이 채널에서는 "
        "정상입니다.\n"
        "이 채널에서도 다음은 위반으로 판단합니다:\n"
        "- 제보 수준을 넘어선 욕설·조롱·패드립 등 모욕 발언\n"
        "- 신고 대상의 게임 닉네임이 아닌 현실 신상 정보(실명, 연락처, SNS 등) 공개\n"
        "- 핵 사용을 옹호·조장하거나 핵 판매/구매를 유도하는 발언"
    ),
}

# ══════════════════════════════════════════════════════════════════
# 수동 검수 모드 (운영 초기 안전장치)
# True: 봇이 위반을 "감지"만 하고 아무 조치(삭제/경고/타임아웃/DM)도 하지 않는다.
#       대신 로그 채널(#제재-로그)에 "자동 모드였다면 어떤 조치가 나갔을지"를 올려주므로,
#       관리자가 판단 정확도를 지켜보다가 믿을 만해지면 False로 바꾸면 된다.
# False: 기존대로 자동 조치 실행.
# 주의: 값을 바꾼 뒤에는 봇 재시작 필요. 검수 모드 동안에는 위반 점수도 쌓이지 않는다.
# ══════════════════════════════════════════════════════════════════
MANUAL_REVIEW_MODE = True

# ── AI 판단 등급별 부여 점수 ─────────────────────────────────────────
# AI는 메시지를 아래 5개 등급 중 하나로 분류합니다.
VIOLATION_LEVEL_POINTS = {
    "NONE": 0,        # 위반 없음
    "MINOR": 1,       # 경미 (예: 가벼운 무례함)
    "MODERATE": 3,    # 중간 (예: 욕설, 광고성 스팸)
    "SEVERE": 6,      # 심각 (예: 혐오발언, 개인정보 유출, 괴롭힘)
    "EXTREME": 12,    # 매우 심각 (예: 노골적 위협, 음란물, 심각한 혐오)
}

# ── 누적 점수(strike)에 따른 제재 단계 ────────────────────────────────
# (누적 점수 임계값, 조치, 지속시간(분, 타임아웃일 때만 사용))
STRIKE_THRESHOLDS = [
    (1,  "WARN",    None),   # 1점 이상: 경고만
    (3,  "DELETE",  None),   # 3점 이상: 메시지 삭제 + 경고
    (6,  "TIMEOUT", 60),     # 6점 이상: 삭제 + 1시간 타임아웃
    (12, "TIMEOUT", 1440),   # 12점 이상: 삭제 + 24시간 타임아웃
    (20, "KICK",    None),   # 20점 이상: 킥
    (30, "BAN",     None),   # 30점 이상: 밴
]

# EXTREME 등급은 누적 점수와 무관하게 즉시 아래 조치를 적용 (선택적 강제 조치)
IMMEDIATE_ACTION_FOR_EXTREME = "TIMEOUT"  # None으로 두면 비활성화, 혹은 "KICK"/"BAN"으로 변경 가능
IMMEDIATE_TIMEOUT_MINUTES = 1440

# 점수 자동 감소 (선택): 마지막 위반 후 N일이 지나면 점수 절반으로 감소
STRIKE_DECAY_DAYS = 30
STRIKE_DECAY_RATIO = 0.5

# 사용할 모델 (1차: Gemini, 2차 폴백: Groq)
GEMINI_MODEL = "gemini-2.5-flash"
GROQ_MODEL = "openai/gpt-oss-120b"

# ── 킥/밴 자동 실행 제한 (커뮤니티 정책: 경고 2회 이후 제재는 운영진이 최종 결정) ──
# 실제 서버 정책상 킥/밴처럼 되돌리기 힘든 조치는 운영진 확인 후 결정되어야 하므로,
# 판단 주체(1차 필터/Gemini/Groq 무관하게) KICK/BAN이 나오면 자동 실행 대신
# 아래 상한(AUTO_ACTION_CEILING)으로 낮춰 실행하고, 관리자 검토대기 목록에 올린다.
# 실제 킥/밴은 !BB 검토대기 로 확인 후 관리자가 수동으로 처리한다.
AUTO_ACTION_CEILING = "TIMEOUT"          # 자동으로 실행 가능한 최고 조치
AUTO_ACTION_CEILING_TIMEOUT_MINUTES = 1440   # 위 상한 적용 시 타임아웃 길이(분) = 24시간
# 하향 조정된 경우, 로그 채널에 "관리자 검토 필요"로 강조 표시
FLAG_DOWNGRADED_FOR_REVIEW = True
# 하향 조정된 경우, 로그 채널에서 추가로 멘션할 대상
# (예: "<@&역할ID>" 또는 "<@유저ID>"). 비워두면 멘션 없이 임베드만 강조 표시.
ADMIN_REVIEW_MENTION = ""

# 운영 정책상 봇 역할에 Administrator 권한을 유지하는 경우 True.
# True이면 로그인 시 과도 권한 경고를 생략하지만 필수 권한 누락 검사는 계속 수행한다.
ALLOW_ADMINISTRATOR_PERMISSION = True

# ── 익명화된 제재 로그 공개 (운영 투명성) ─────────────────────────────
# 커뮤니티 공지의 "제재 로그 공개 기능"에 해당: 경고/제재가 이루어질 때
# 특정 유저를 지칭하지 않는 범위(닉네임/ID/멘션/원문 제외)에서 위반 사유와
# 조치 내용만 공개 채널에 게시한다. 특정인을 비난하기 위한 목적이 아니라
# 운영 기준을 투명하게 안내하기 위한 목적이다.
# 사용하려면 .env에 PUBLIC_LOG_CHANNEL_ID를 설정하세요. 비워두면 비활성화.
PUBLIC_SANCTION_LOG_ENABLED = True
# 공개 로그에 올릴 최소 등급 (이 미만의 경미한 위반은 공개하지 않음)
PUBLIC_LOG_MIN_LEVEL = "MODERATE"   # "MINOR" | "MODERATE" | "SEVERE" | "EXTREME"

# ── 대규모 서버용 성능/비용 설정 ─────────────────────────────────────
# 동시에 처리할 수 있는 최대 외부 AI 호출 수 (트래픽이 몰려도 이 이상 동시 호출 안 함)
MAX_CONCURRENT_AI_CALLS = 8

# 메시지 처리 대기열(큐)의 최대 크기. 초과분은 버리고 로그만 남김 (폭주 시 봇 다운 방지)
MAX_QUEUE_SIZE = 5000

# 이 시간보다 오래 큐에서 기다린 메시지는 현재 맥락과 달라졌을 가능성이 높아 폐기한다.
MAX_QUEUE_AGE_SECONDS = 120

# 동일/유사 메시지 판단 결과를 캐시해두는 시간(초). 도배/매크로 스팸에 특히 효과적
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 20000

# ── 오탐 학습 (검수 카드의 "✅ 정상 (조치 안 함)" 버튼과 연동) ──────────
# 관리자가 오탐으로 확정한 메시지는 DB에 저장되어 (봇 재시작에도 유지):
# 1) 동일한 내용(공백/대소문자 무시)이 다시 올라오면 감지 자체를 건너뛰고,
# 2) 최근 사례 N건이 AI 프롬프트에 "오탐 예시"로 포함되어 비슷한 유형의 오탐도 줄인다.
FALSE_POSITIVE_PROMPT_EXAMPLES = 15      # 프롬프트에 포함할 최근 오탐 사례 수 (0 = 프롬프트 학습 비활성)
FALSE_POSITIVE_EXAMPLE_MAX_CHARS = 120   # 사례 하나당 프롬프트에 넣을 원문 길이 제한
FALSE_POSITIVE_REFRESH_SECONDS = 300     # 오탐 사례 목록을 DB에서 다시 읽는 주기(초)

# ── AI 판단 장애 알림 ────────────────────────────────────────────────
# Gemini와 Groq가 둘 다 실패하면 메시지는 안전하게 "위반 없음" 처리되지만(무고한 제재 방지),
# 이 상태가 계속되면 사실상 무감시 상태다. AI 판단이 아래 횟수만큼 연속으로 실패하면
# 로그 채널(#제재-로그)에 경고를 올려 관리자가 알 수 있게 한다.
AI_OUTAGE_ALERT_THRESHOLD = 5
# 장애가 길어져도 이 간격(분)보다 자주 경고를 반복하지는 않음
AI_OUTAGE_ALERT_COOLDOWN_MINUTES = 60

# ── 메시지 누락(드롭) 알림 ───────────────────────────────────────────
# 큐가 가득 차거나(MAX_QUEUE_SIZE 초과) 큐에서 너무 오래 대기해(MAX_QUEUE_AGE_SECONDS)
# 검사되지 못하고 버려진 메시지는 사실상 감시 구멍이다. 콘솔에만 찍히면 놓치기 쉬우므로,
# 누적 드롭이 아래 개수를 넘을 때마다 로그 채널에 경고를 올린다.
DROP_ALERT_THRESHOLD = 50
# 드롭이 계속돼도 이 간격(분)보다 자주 경고를 반복하지는 않음
DROP_ALERT_COOLDOWN_MINUTES = 30

# ── 1차 필터(키워드/패턴) 설정 ────────────────────────────────────────
# 여기 걸리면 AI 호출 없이 즉시 처리됩니다.
# 단어는 반드시 따옴표로 감싸고, 여러 개면 쉼표로 구분하세요. 예: ["단어1", "단어2"]
#
# [주의: 부분 문자열 매칭] 메시지 안에 단어가 "포함"만 돼 있어도 걸립니다.
# 그래서 일상 단어에 포함될 수 있는 표현은 일부러 뺐습니다 — 이런 건 AI가 맥락으로 판단합니다:
#   "꺼져"(불이 꺼져요), "닥쳐"(위기가 닥쳐온다), "새끼" 단독(새끼손가락),
#   "죽어/죽여"(FPS 게임 대화에서 일상적), "미친" 단독(미친 재미), "걸레" 단독(청소)
# 단어를 추가할 때도 "이 글자들이 평범한 단어 안에 들어가진 않나?"를 꼭 생각해 보세요.

# 심각(SEVERE, 6점): 걸리면 즉시 메시지 삭제 + 1시간 타임아웃.
# 패드립, 혐오/차별 표현, 강한 인신공격 등 오해의 여지가 없는 것만 넣습니다.
BANNED_WORDS_SEVERE = [
    # 패드립 (가족 모욕)
    "느금", "니애미", "니에미", "니미럴", "애미없", "애미뒤", "니애미", "니에미", "니미럴", "애미없", "애미뒤", "니엄",
    "엄뒤련",
    # 성적 모욕
    "창녀", "걸레년", "씹창녀", "씹창년", "씹걸레", "씹걸레년", "씹걸레년놈", "씹걸레년새끼", "씹걸레년년놈", "씹걸레년년새끼",
    # 혐오/차별 표현 (국적·지역·성별)
    "짱깨", "짱께", "쪽바리", "쪽발이", "전라디언", "한남충", "김치녀", "홍어", "홍어새끼",
    "홍어년", "홍어놈", "홍어충", "홍어새끼", "홍어년", "홍어놈", "홍어충", "한남", "한남새끼", "한남년", "한남놈", "한녀", "한녀충", "한녀새끼", "한녀년", "한녀놈",
    # 강한 인신공격 (욕설 + 대상 결합형)
    "씨발년", "씨발놈", "씹새끼", "개새끼", "병신새끼",
    # 영어 슬러 (인종·성소수자 비하)
    "nigger", "nigga", "faggot",
]

# 경미(MODERATE 목록이지만 MINOR 1점 = 경고만): 일반 욕설/비속어.
# 서버 정책(경고 2회 누적 후 제재)에 맞춰 처음엔 경고만 주고 반복 시 자동 격상됩니다.
BANNED_WORDS_MODERATE = [
    # 일반 욕설
    "씨발", "시발", "씨팔", "씨빨", "병신", "븅신", "빙신",
    "지랄", "존나", "좆", "썅", "새꺄", "등신", "아가리",
    "미친놈", "미친년", "미친새끼", "개소리", "엿먹", "좆같", "좆나", "좆밥", "좆문가", "좆문", "좆까", "좆까라",
    # 초성 축약형
    "ㅅㅂ", "ㅆㅂ", "ㅂㅅ", "ㅄ", "ㅈㄹ", "ㅈㄴ", "ㄴㄱㅁ", "ㅅㄲ", "ㄴㅇㅁ", "ㅇㅈㄹ", "ㅈㄹㄴ", "ㅈㄴㄴ", "ㅁㅊ", "ㅁㅊㄴ", "ㅁㅊㅅ", "ㄱㅅㄲ", "ㅇㅅㄲ",
    # 영어 욕설
    "fuck", "bitch",
    # 참고: "시발"은 "시발점(출발점)" 같은 정상 단어에도 걸릴 수 있지만 경고 1점이라
    # 피해가 작고, 채팅에서는 욕설 빈도가 압도적이라 포함했습니다. 오탐이 거슬리면 빼세요.
]

# 같은 유저가 이 시간(초) 안에 동일/유사 메시지를 이 횟수 이상 보내면 스팸으로 간주
SPAM_WINDOW_SECONDS = 10
SPAM_REPEAT_THRESHOLD = 10

# 초대 링크/외부 링크 자동 감지 (정규식은 filters.py에서 사용)
BLOCK_DISCORD_INVITES = True

# 이 길이 이하의 메시지는 AI 호출 없이 바로 통과 (이모지, 짧은 반응 등 비용 절감)
MIN_LENGTH_FOR_AI_CHECK = 4

# ══════════════════════════════════════════════════════════════════
# 배치 감사(Batch Audit) 설정
# 실시간 자동제재와 별개로, 특정 채널들의 대화를 주기적으로 모아서
# 운영 규정에 따라 분류/태깅한 뒤 "리포트"만 만든다 (조치는 자동 실행하지 않음).
# ══════════════════════════════════════════════════════════════════

# 감사 대상 채널 ID 목록. .env의 WATCHED_CHANNEL_IDS가 있으면 해당 값을 우선 사용한다.
# 환경변수를 빈 값으로 두면 배치 감사를 비활성화할 수 있다.
_DEFAULT_WATCHED_CHANNEL_IDS = [
    1441312515828224101,  # 자유
    719027398028296282,   # PVP
    1412298919043661924,  # PVE
    1412299355431632987,  # Arena
    1475842559275438161,  # PVE-TTS
]
_watched_channel_ids_env = os.environ.get("WATCHED_CHANNEL_IDS")
WATCHED_CHANNEL_IDS = (
    _DEFAULT_WATCHED_CHANNEL_IDS
    if _watched_channel_ids_env is None
    else _parse_channel_id_list(_watched_channel_ids_env)
)


# 통합 실행(봇 상시 가동) 모드일 때 배치 감사를 실행할 시각 (24시간제, 한국 시간).
# 예전에는 "봇 시작 후 24시간마다"여서 봇을 재시작할 때마다 감사가 즉시 실행돼
# 무료 API 한도를 낭비했다. 이제는 재시작과 무관하게 매일 이 시각에 한 번만 돈다.
BATCH_RUN_HOUR_KST = 4   # 새벽 4시 (채팅이 가장 적은 시간대)

# 배치 감사에서 "위반 의심"으로 걸린 메시지를, 감사 리포트뿐 아니라
# 제재 로그 채널(#제재-로그)에도 검토 버튼 카드로 올릴지 여부.
# 배치 감사는 원래 자동 조치를 하지 않으므로, 관리자가 카드의 버튼(삭제/타임아웃/킥/밴 등)으로
# 직접 조치할 수 있게 해준다. (실시간 검수 카드와 동일한 형태)
BATCH_POST_REVIEW_CARDS = True

# 한 번의 감사에서 올릴 검토 카드의 최대 개수 (첫 실행 등에서 대량 도배 방지).
# 심각한 등급부터 카드로 올리고, 초과분은 리포트 파일/채널에서 확인.
BATCH_REVIEW_CARD_LIMIT = 25

# 한 번의 LLM 호출에 묶어서 보낼 메시지 개수 (너무 크면 응답 파싱 실패율↑, 너무 작으면 호출 수↑)
BATCH_SIZE = 25

# 배치 판단에 사용할 기본 백엔드: "auto"(Gemini→Groq 자동 폴백) | "gemini" | "groq" | "ollama"
# 독립 실행 스크립트(batch_audit.py)에서는 --backend 인자로 매번 덮어쓸 수 있다.
BATCH_BACKEND = "auto"

# 로컬 Ollama 설정 (개인 PC에서 주기적으로 로컬 GPU로 돌릴 때 사용)
OLLAMA_BASE_URL = "http://localhost:11434"
# 한국어 뉘앙스 판단이 중요하므로 한국어 성능이 검증된 모델 권장.
# VRAM 여유가 있으면 더 큰 모델로, 부족하면 작은 모델로 바꾸세요.
OLLAMA_MODEL = "qwen2.5:14b"

# 완성된 리포트를 저장할 로컬 폴더 (Markdown 파일)
REPORT_OUTPUT_DIR = "./reports"

# 체크포인트가 없는 "첫 실행" 시 얼마나 과거까지 수집할지 (일 단위).
# 제한 없이 채널 전체 이력을 긁으면 대형 서버에서 수십만 건을 가져와
# 실행이 몇 시간씩 걸리고 디스코드 API 한도에 걸릴 수 있다.
BATCH_FIRST_RUN_LOOKBACK_DAYS = 7

# 한 채널에서 한 번의 감사 실행으로 수집하는 메시지 수 상한 (첫 실행 포함 폭주 방지)
BATCH_MAX_MESSAGES_PER_CHANNEL = 3000

# 위반 로그 원문 보존 기간(일). 0이면 자동 익명화를 하지 않는다.
# 양수로 설정하면 봇 시작 시 해당 기간보다 오래된 완료 기록의 message_content를 비운다.
# pending/processing 검수와 오탐 학습 데이터는 판단·학습에 필요하므로 대상에서 제외한다.
VIOLATION_CONTENT_RETENTION_DAYS = 90

# 로컬 감사 리포트 보존 기간(일). 0이면 자동 삭제하지 않는다.
# 양수일 때만 REPORT_OUTPUT_DIR의 audit_report_*.md 파일을 대상으로 한다.
REPORT_RETENTION_DAYS = 180


def validate_config() -> None:
    """운영 중 무감시·과잉 제재를 만들 수 있는 잘못된 설정을 시작 시 차단한다."""
    errors = []
    valid_levels = {"NONE", "MINOR", "MODERATE", "SEVERE", "EXTREME"}
    valid_actions = {"NONE", "WARN", "DELETE", "TIMEOUT", "KICK", "BAN"}
    valid_batch_backends = {"auto", "gemini", "groq", "ollama"}

    if MAX_CONCURRENT_AI_CALLS <= 0:
        errors.append("MAX_CONCURRENT_AI_CALLS는 1 이상이어야 합니다.")
    if DROP_ALERT_THRESHOLD <= 0 or DROP_ALERT_COOLDOWN_MINUTES <= 0:
        errors.append("DROP_ALERT_THRESHOLD와 DROP_ALERT_COOLDOWN_MINUTES는 1 이상이어야 합니다.")
    if MAX_QUEUE_SIZE <= 0 or MAX_QUEUE_AGE_SECONDS <= 0:
        errors.append("큐 크기와 최대 대기시간은 1 이상이어야 합니다.")
    if CACHE_TTL_SECONDS <= 0 or CACHE_MAX_ENTRIES <= 0:
        errors.append("캐시 TTL과 최대 항목 수는 1 이상이어야 합니다.")
    if (FALSE_POSITIVE_PROMPT_EXAMPLES < 0 or FALSE_POSITIVE_EXAMPLE_MAX_CHARS <= 0
            or FALSE_POSITIVE_REFRESH_SECONDS <= 0):
        errors.append("오탐 학습 설정(FALSE_POSITIVE_*) 값이 올바르지 않습니다.")
    if SPAM_WINDOW_SECONDS <= 0 or not 2 <= SPAM_REPEAT_THRESHOLD <= 20:
        errors.append("스팸 시간은 양수이고 반복 임계값은 2~20이어야 합니다.")
    if MIN_LENGTH_FOR_AI_CHECK < 0:
        errors.append("MIN_LENGTH_FOR_AI_CHECK는 0 이상이어야 합니다.")
    if BATCH_SIZE <= 0 or BATCH_MAX_MESSAGES_PER_CHANNEL <= 0:
        errors.append("배치 크기와 채널별 최대 메시지 수는 1 이상이어야 합니다.")
    if BATCH_FIRST_RUN_LOOKBACK_DAYS <= 0 or BATCH_REVIEW_CARD_LIMIT < 0:
        errors.append("배치 조회 기간은 양수이고 검토 카드 상한은 0 이상이어야 합니다.")
    if (isinstance(VIOLATION_CONTENT_RETENTION_DAYS, bool)
            or not isinstance(VIOLATION_CONTENT_RETENTION_DAYS, int)
            or VIOLATION_CONTENT_RETENTION_DAYS < 0):
        errors.append("VIOLATION_CONTENT_RETENTION_DAYS는 0 이상의 정수여야 합니다.")
    if (isinstance(REPORT_RETENTION_DAYS, bool)
            or not isinstance(REPORT_RETENTION_DAYS, int)
            or REPORT_RETENTION_DAYS < 0):
        errors.append("REPORT_RETENTION_DAYS는 0 이상의 정수여야 합니다.")
    if BATCH_BACKEND not in valid_batch_backends:
        errors.append("BATCH_BACKEND는 auto/gemini/groq/ollama 중 하나여야 합니다.")
    if not isinstance(REPORT_OUTPUT_DIR, str) or not REPORT_OUTPUT_DIR.strip():
        errors.append("REPORT_OUTPUT_DIR는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(OLLAMA_BASE_URL, str) or not OLLAMA_BASE_URL.startswith(("http://", "https://")):
        errors.append("OLLAMA_BASE_URL은 http:// 또는 https://로 시작해야 합니다.")
    if not all(isinstance(model, str) and model.strip()
               for model in (GEMINI_MODEL, GROQ_MODEL, OLLAMA_MODEL)):
        errors.append("AI 모델 이름은 비어 있지 않은 문자열이어야 합니다.")
    if not 0 <= BATCH_RUN_HOUR_KST <= 23:
        errors.append("BATCH_RUN_HOUR_KST는 0~23이어야 합니다.")
    if not 0 < STRIKE_DECAY_RATIO <= 1 or STRIKE_DECAY_DAYS <= 0:
        errors.append("점수 감쇠 기간은 양수이고 감쇠 비율은 0 초과 1 이하여야 합니다.")
    if set(VIOLATION_LEVEL_POINTS) != valid_levels:
        errors.append("VIOLATION_LEVEL_POINTS에는 5개 표준 등급이 모두 있어야 합니다.")
    elif any(isinstance(points, bool) or not isinstance(points, (int, float)) or points < 0
             for points in VIOLATION_LEVEL_POINTS.values()):
        errors.append("VIOLATION_LEVEL_POINTS의 점수는 0 이상의 숫자여야 합니다.")
    if PUBLIC_LOG_MIN_LEVEL not in valid_levels - {"NONE"}:
        errors.append("PUBLIC_LOG_MIN_LEVEL 값이 올바르지 않습니다.")
    if AUTO_ACTION_CEILING not in {"WARN", "DELETE", "TIMEOUT"}:
        errors.append("AUTO_ACTION_CEILING은 WARN/DELETE/TIMEOUT 중 하나여야 합니다.")
    if not isinstance(ALLOW_ADMINISTRATOR_PERMISSION, bool):
        errors.append("ALLOW_ADMINISTRATOR_PERMISSION은 True 또는 False여야 합니다.")
    if IMMEDIATE_ACTION_FOR_EXTREME not in valid_actions | {None}:
        errors.append("IMMEDIATE_ACTION_FOR_EXTREME 값이 올바르지 않습니다.")
    threshold_rows_valid = all(isinstance(row, (tuple, list)) and len(row) == 3
                               for row in STRIKE_THRESHOLDS)
    if not threshold_rows_valid:
        errors.append("STRIKE_THRESHOLDS의 각 항목은 (점수, 조치, 시간) 형식이어야 합니다.")
    else:
        thresholds = [row[0] for row in STRIKE_THRESHOLDS]
        numeric_thresholds = all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0
            for value in thresholds
        )
        if not numeric_thresholds:
            errors.append("STRIKE_THRESHOLDS 임계값은 0 이상의 숫자여야 합니다.")
        elif thresholds != sorted(thresholds) or len(thresholds) != len(set(thresholds)):
            errors.append("STRIKE_THRESHOLDS 임계값은 중복 없이 오름차순이어야 합니다.")
        if any(row[1] not in valid_actions for row in STRIKE_THRESHOLDS):
            errors.append("STRIKE_THRESHOLDS에 알 수 없는 조치가 있습니다.")
        if any((row[1] == "TIMEOUT" and
                (isinstance(row[2], bool) or not isinstance(row[2], (int, float)) or row[2] <= 0))
               for row in STRIKE_THRESHOLDS):
            errors.append("TIMEOUT 조치에는 0보다 큰 제한 시간(분)이 필요합니다.")
    if AUTO_ACTION_CEILING == "TIMEOUT" and AUTO_ACTION_CEILING_TIMEOUT_MINUTES <= 0:
        errors.append("자동 조치 상한이 TIMEOUT이면 제한 시간은 0보다 커야 합니다.")
    if IMMEDIATE_ACTION_FOR_EXTREME == "TIMEOUT" and IMMEDIATE_TIMEOUT_MINUTES <= 0:
        errors.append("EXTREME 즉시 조치가 TIMEOUT이면 제한 시간은 0보다 커야 합니다.")
    if any(isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id <= 0
           for channel_id in WATCHED_CHANNEL_IDS):
        errors.append("WATCHED_CHANNEL_IDS에는 양의 정수 채널 ID만 사용할 수 있습니다.")
    if len(WATCHED_CHANNEL_IDS) != len(set(WATCHED_CHANNEL_IDS)):
        errors.append("WATCHED_CHANNEL_IDS에 중복 채널이 있습니다.")

    if errors:
        raise ValueError("설정 오류:\n- " + "\n- ".join(errors))
