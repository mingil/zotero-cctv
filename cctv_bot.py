import os
import re
import sys
import json
import time
import signal
import argparse
import tempfile
import logging
from logging.handlers import RotatingFileHandler
import requests
import schedule
from pyzotero import zotero

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# 1. 환경 설정 및 스마트 경로 감지
# ==========================================
VERSION = "2.0.0"

USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user").strip().lower() or "user"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def _parse_bool(val, default=False):
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on", "y")

def _parse_float(val, default=6.0):
    try:
        v = float(val)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default

SCAN_INTERVAL_HOURS = _parse_float(os.environ.get("SCAN_INTERVAL_HOURS"), 6.0)
ALERT_ON_DELETE = _parse_bool(os.environ.get("ALERT_ON_DELETE"), True)
ALERT_ON_ADD = _parse_bool(os.environ.get("ALERT_ON_ADD"), False)
ALERT_ON_MOVE = _parse_bool(os.environ.get("ALERT_ON_MOVE"), False)

def resolve_data_paths():
    """
    기존 루트 마운트(.:/app) 방식과 신규 데이터 폴더 마운트(./data:/app/data) 방식을
    모두 100% 호환하도록 상태 파일 및 로그 파일 경로를 결정합니다.
    """
    env_log = os.environ.get("LOG_FILE", "").strip()
    env_state = os.environ.get("STATE_FILE", "").strip()
    env_data_dir = os.environ.get("DATA_DIR", "").strip()

    default_log_name = "zotero_audit_global.log"
    default_state_name = "zotero_state_global.json"

    if env_data_dir:
        os.makedirs(env_data_dir, exist_ok=True)
        base_dir = env_data_dir
    elif os.path.exists(default_state_name) or os.path.exists(default_log_name):
        base_dir = "."
    elif os.path.isdir("data"):
        base_dir = "data"
    else:
        base_dir = "."

    log_path = env_log if env_log else os.path.join(base_dir, default_log_name)
    state_path = env_state if env_state else os.path.join(base_dir, default_state_name)

    # 앞의 불필요한 './' 정리 (기존 파일명 호환성 유지)
    if log_path.startswith("./"):
        log_path = log_path[2:]
    if state_path.startswith("./"):
        state_path = state_path[2:]

    return log_path, state_path

LOG_FILE, STATE_FILE = resolve_data_paths()

NON_BIBLIO_TYPES = {"attachment", "note", "annotation"}
UNKNOWN_FOLDER_RE = re.compile(r"^알수없는폴더\(([A-Z0-9]+)\)$")

# Graceful Shutdown 상태 플래그
STOP_REQUESTED = False

def _signal_handler(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.info(f"🛑 종료 신호(Signal {signum}) 수신. 안전하게 스케줄러를 종료합니다...")

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ==========================================
# 2. 로깅 최적화 (Log Rotation)
# ==========================================
logger = logging.getLogger("ZoteroCCTV")
logger.setLevel(logging.INFO)

if not logger.handlers:
    log_dir = os.path.dirname(os.path.abspath(LOG_FILE))
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    console_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

def log_message(msg):
    logger.info(msg)

def log_error(msg):
    logger.error(msg)

# ==========================================
# 3. JSON 파일 안전 쓰기 (Atomic Write)
# ==========================================
def safe_write_json(data, filepath):
    """임시 파일에 저장 후 원본을 원자적으로 덮어씌워 데이터 유실 및 깨짐을 방지합니다."""
    dirname = os.path.dirname(os.path.abspath(filepath)) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dirname, prefix="temp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    except Exception as e:
        log_error(f"⚠️ 상태 파일 저장 중 오류 발생 ({type(e).__name__}): {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

# ==========================================
# 4. 네트워크 재시도 & 텔레그램 푸시 알림
# ==========================================
def retry_api_call(func, max_retries=3, base_delay=5):
    """일시적인 DNS 오류나 네트워크 순단 발생 시 지수 백오프로 자동 재시도합니다."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait_sec = base_delay * (2 ** (attempt - 1))
                log_error(
                    f"⚠️ Zotero API 일시 통신 지연 ({attempt}/{max_retries}회차, {type(e).__name__}: {e}) "
                    f"-> {wait_sec}초 후 재시도합니다..."
                )
                time.sleep(wait_sec)
    raise last_err

def send_telegram_alert(msg, dry_run=False):
    """텔레그램 봇 알림을 전송합니다 (4000자 초과 시 분할 전송 및 429 Rate Limit 방어)."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    if dry_run:
        log_message(f"🧪 [Dry-Run 텔레그램 발송 생략] {msg}")
        return True

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    max_len = 4000
    chunks = [msg[i : i + max_len] for i in range(0, len(msg), max_len)] or [msg]

    success = True
    for chunk in chunks:
        payload = {"chat_id": TG_CHAT_ID, "text": chunk}
        for attempt in range(1, 3):
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 429:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
                    time.sleep(min(retry_after + 1, 15))
                    continue
                response.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    log_error(f"⚠️ 텔레그램 발송 에러 ({type(e).__name__}): {e}")
                    success = False
                else:
                    time.sleep(2)
    return success

# ==========================================
# 5. 유틸리티: 문헌 판별 및 폴더명 자가 치유
# ==========================================
def is_trackable_top_item(item_data):
    """최상위 학술 문헌(논문/도서 등)인지 확인합니다. 하위 첨부파일, 노트, 주석은 제외합니다."""
    if not isinstance(item_data, dict):
        return False
    if item_data.get("parentItem"):
        return False
    item_type = item_data.get("itemType", "")
    if item_type in NON_BIBLIO_TYPES:
        return False
    return True

def extract_item_title(item_data):
    """문헌 제목을 추출합니다 (대체 제목 필드 지원)."""
    for field in ("title", "caseName", "nameOfAct", "subject", "shortTitle"):
        val = item_data.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return "제목 없음"

def heal_unknown_collections(state_dict, col_map):
    """
    과거 100개 폴더 페이지네이션 제한으로 인해 '알수없는폴더(KEY)'로 저장되었던 항목들을
    실제 폴더 이름으로 자동 복구(Self-Healing)합니다.
    """
    healed_count = 0
    for key, info in state_dict.items():
        cols = info.get("collections", [])
        if not cols:
            continue
        new_cols = []
        changed = False
        for c in cols:
            m = UNKNOWN_FOLDER_RE.match(str(c))
            if m:
                col_key = m.group(1)
                if col_key in col_map:
                    new_cols.append(col_map[col_key])
                    changed = True
                    healed_count += 1
                else:
                    new_cols.append(c)
            else:
                new_cols.append(c)
        if changed:
            info["collections"] = sorted(list(set(new_cols)))
    return healed_count

def load_local_state(filepath=STATE_FILE):
    """로컬 상태 파일을 불러옵니다 (구버전 및 신버전 포맷 완벽 호환)."""
    past_state = {}
    last_version = 0
    past_col_map = {}

    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "library_version" in data and "items" in data:
                    past_state = data.get("items", {})
                    last_version = int(data.get("library_version", 0))
                    past_col_map = data.get("collections_map", {})
                elif isinstance(data, dict):
                    past_state = data
                    last_version = 0
        except Exception as e:
            log_error(f"⚠️ {filepath} 손상됨 ({type(e).__name__}: {e}). 처음부터 다시 스캔합니다.")
    return past_state, last_version, past_col_map

# ==========================================
# 6. 핵심: Zotero API 통신 최적화 (버전 동기화)
# ==========================================
def run_audit(zot, dry_run=False, force_heal=False):
    log_message("🔍 [정기 스캔] Zotero 라이브러리 스캔 시작...")

    # 1. 상태 불러오기
    past_state, last_version, past_col_map = load_local_state(STATE_FILE)

    try:
        # 2. 서버의 현재 최신 버전 확인 (아주 가벼운 API 호출)
        server_version = retry_api_call(lambda: zot.last_modified_version())

        # 기존 상태에 '알수없는폴더(...)'가 남아있는지 점검
        has_unknown_folders = any(
            any(UNKNOWN_FOLDER_RE.match(str(c)) for c in info.get("collections", []))
            for info in past_state.values()
        )

        # 만약 로컬 버전과 서버 버전이 같고, 복구할 폴더도 없으면 0.1초 만에 종료!
        if last_version == server_version and last_version != 0 and not has_unknown_folders and not force_heal:
            log_message(f"💤 변경된 항목이 없습니다. 현재 버전(v{server_version}) 유지.")
            log_message("💤 [스캔 완료] 다음 스케줄까지 대기합니다.\n")
            return

        # 3. 전체 폴더(컬렉션) 이름 매핑용 조회 (100개 초과 폴더까지 전수 조회!)
        collections = retry_api_call(lambda: zot.everything(zot.collections()))
        live_col_map = {
            c["key"]: c["data"]["name"]
            for c in collections
            if isinstance(c, dict) and "key" in c and "data" in c and "name" in c["data"]
        }
        # 삭제된 폴더 이름도 기억할 수 있도록 과거 맵과 병합 (현재 이름 우선)
        col_map = {**past_col_map, **live_col_map}

        current_state = {
            k: {"title": v.get("title", "제목 없음"), "collections": list(v.get("collections", []))}
            for k, v in past_state.items()
        }

        # 과거 '알수없는폴더(KEY)' 자가 치유 실행
        healed_refs = heal_unknown_collections(current_state, col_map)
        if healed_refs > 0:
            log_message(f"🛠️ [자가 치유] 기존 상태 파일의 미확인 폴더 참조 {healed_refs}건을 실제 폴더명으로 복구했습니다.")

        # --heal 실행 시 과거 구버전에서 잘못 유입된 독립 노트(제목 없음) 항목 정리
        if force_heal:
            untitled_keys = [k for k, v in current_state.items() if v.get("title") == "제목 없음"]
            removed_notes = 0
            for i in range(0, len(untitled_keys), 50):
                batch_keys = untitled_keys[i : i + 50]
                fetched = retry_api_call(lambda bk=batch_keys: zot.items(itemKey=",".join(bk)))
                for it in fetched:
                    if not is_trackable_top_item(it.get("data", {})):
                        if it["key"] in current_state:
                            del current_state[it["key"]]
                            removed_notes += 1
            if removed_notes > 0:
                log_message(f"🧹 [자가 치유] 논문이 아닌 단순 메모/첨부파일 {removed_notes}건을 감시 목록에서 정리했습니다.")

        if last_version == server_version and last_version != 0 and not force_heal:
            if not dry_run:
                save_data = {
                    "library_version": server_version,
                    "collections_map": col_map,
                    "items": current_state,
                }
                safe_write_json(save_data, STATE_FILE)
            log_message(f"💾 폴더명 복구 저장 완료 (현재 버전: v{server_version}). 다음 스케줄까지 대기합니다.\n")
            return

        deleted_alerts = []
        added_alerts = []
        moved_alerts = []

        # 4. 데이터 동기화
        if last_version == 0:
            log_message("📡 [초기 구축 스캔] 전체 논문 데이터를 가져옵니다. (시간이 다소 소요될 수 있습니다)")
            items = retry_api_call(lambda: zot.everything(zot.top()))
            current_state = {}

            for item in items:
                data = item.get("data", {})
                if data.get("deleted") or not is_trackable_top_item(data):
                    continue
                key = item["key"]
                title = extract_item_title(data)
                col_keys = data.get("collections", [])
                col_names = sorted(list({col_map.get(ck, f"알수없는폴더({ck})") for ck in col_keys}))
                current_state[key] = {"title": title, "collections": col_names}

            log_message(f"✅ 초기 스캔 완료. 총 {len(current_state)}개의 논문 감시를 시작합니다.")

        else:
            log_message(f"⚡ [초고속 스캔] v{last_version} -> v{server_version} 변경/추가/삭제 항목만 가져옵니다.")

            # (A) 영구 삭제된 항목 가져오기
            deleted_data = retry_api_call(lambda: zot.deleted(since=last_version))
            deleted_keys = (
                deleted_data.get("items", []) if isinstance(deleted_data, dict) else list(deleted_data or [])
            )

            # (B) 변경(추가/이동/수정/휴지통 이동)된 항목 가져오기
            modified_items = retry_api_call(lambda: zot.everything(zot.items(since=last_version)))

            # 휴지통 이동(deleted: 1)된 항목도 삭제 목록에 통합
            trashed_keys = []
            active_modified_items = []
            for item in modified_items:
                key = item.get("key")
                data = item.get("data", {})
                if data.get("deleted"):
                    trashed_keys.append(key)
                else:
                    active_modified_items.append(item)

            # 🚨 이벤트 1: 삭제 / 휴지통 이동 처리 (중복 제거)
            all_removed_keys = []
            seen_removed = set()
            for k in list(deleted_keys) + trashed_keys:
                if k and k not in seen_removed:
                    seen_removed.add(k)
                    all_removed_keys.append(k)

            for key in all_removed_keys:
                if key in current_state:
                    title = current_state[key]["title"]
                    cols = current_state[key]["collections"]
                    event_type = "휴지통 이동 감지" if key in trashed_keys else "휴지통/삭제 감지"
                    alert_msg = (
                        f"🚨 [{event_type}] '{title}' 논문이 라이브러리에서 사라졌습니다! "
                        f"(기존 폴더: {cols if cols else ['루트(미분류)']})"
                    )
                    log_message(alert_msg)
                    deleted_alerts.append(alert_msg)
                    del current_state[key]

            # ✨/🔄 이벤트 2 & 3: 신규 추가 및 폴더 이동 처리
            for item in active_modified_items:
                key = item["key"]
                data = item.get("data", {})

                if not is_trackable_top_item(data):
                    # 기존에 최상위 항목이었다가 하위 첨부파일/노트로 병합된 경우 감시 목록에서 정리
                    if key in current_state:
                        del current_state[key]
                    continue

                title = extract_item_title(data)
                col_keys = data.get("collections", [])
                col_names = sorted(list({col_map.get(ck, f"알수없는폴더({ck})") for ck in col_keys}))

                if key not in current_state:
                    # ✨ 이벤트 2: 신규 추가
                    add_msg = f"✨ [신규 추가] '{title}' (소속: {col_names if col_names else '루트'})"
                    log_message(add_msg)
                    added_alerts.append(add_msg)
                else:
                    # 🔄 이벤트 3: 폴더 이동
                    past_cols = set(current_state[key]["collections"])
                    curr_cols = set(col_names)
                    if past_cols != curr_cols:
                        added = sorted(list(curr_cols - past_cols))
                        removed = sorted(list(past_cols - curr_cols))
                        log_message(f"🔄 [폴더 이동] '{title}'")
                        if added:
                            log_message(f"   -> 📥 IN: {added}")
                        if removed:
                            log_message(f"   -> 📤 OUT: {removed}")
                        move_msg = f"🔄 [폴더 이동] '{title}' (IN: {added}, OUT: {removed})"
                        moved_alerts.append(move_msg)

                # 상태 업데이트
                current_state[key] = {"title": title, "collections": col_names}

        # 5. 텔레그램 알림 발송 (개별 또는 대량 묶음 처리)
        if ALERT_ON_DELETE and deleted_alerts:
            if len(deleted_alerts) <= 3:
                for alert in deleted_alerts:
                    send_telegram_alert(alert, dry_run=dry_run)
            else:
                summary_msg = (
                    f"🚨 [대량 삭제/휴지통 이동 감지] 총 {len(deleted_alerts)}개의 논문이 사라졌습니다!\n\n"
                    + "\n".join(f"• {a.replace('🚨 ', '')}" for a in deleted_alerts)
                )
                send_telegram_alert(summary_msg, dry_run=dry_run)

        if ALERT_ON_ADD and added_alerts:
            summary_msg = f"✨ [신규 논문 {len(added_alerts)}건 추가]\n" + "\n".join(added_alerts[:20])
            if len(added_alerts) > 20:
                summary_msg += f"\n...외 {len(added_alerts) - 20}건"
            send_telegram_alert(summary_msg, dry_run=dry_run)

        if ALERT_ON_MOVE and moved_alerts:
            summary_msg = f"🔄 [논문 폴더 이동 {len(moved_alerts)}건]\n" + "\n".join(moved_alerts[:20])
            if len(moved_alerts) > 20:
                summary_msg += f"\n...외 {len(moved_alerts) - 20}건"
            send_telegram_alert(summary_msg, dry_run=dry_run)

        # 6. 최종 상태 저장
        if not dry_run:
            save_data = {
                "library_version": server_version,
                "collections_map": col_map,
                "items": current_state,
            }
            safe_write_json(save_data, STATE_FILE)
            log_message(f"💾 스캔 및 저장 완료 (현재 버전: v{server_version}, 총 {len(current_state)}편 감시 중). 다음 스케줄까지 대기합니다.\n")
        else:
            log_message(f"🧪 [Dry-Run 완료] 서버 버전: v{server_version}, 감시 대상: {len(current_state)}편 (파일 저장 생략)\n")

    except Exception as e:
        log_error(f"⚠️ Zotero 동기화 중 오류 발생 ({type(e).__name__}): {e}\n")

# ==========================================
# 7. 상태 진단 및 CLI 도구
# ==========================================
def print_status():
    """현재 로컬 상태 파일 및 환경 설정 정보를 출력합니다."""
    past_state, last_version, past_col_map = load_local_state(STATE_FILE)
    unknown_count = sum(
        1
        for info in past_state.values()
        if any(UNKNOWN_FOLDER_RE.match(str(c)) for c in info.get("collections", []))
    )
    print("==========================================")
    print(f"📊 Zotero Global CCTV Status (v{VERSION})")
    print("==========================================")
    print(f"• 상태 파일 경로 : {os.path.abspath(STATE_FILE)}")
    print(f"• 로그 파일 경로 : {os.path.abspath(LOG_FILE)}")
    print(f"• 라이브러리 버전: v{last_version}")
    print(f"• 감시 중인 논문 : {len(past_state):,}편")
    print(f"• 캐시된 폴더 수 : {len(past_col_map):,}개")
    print(f"• 미확인 폴더 수 : {unknown_count:,}편")
    print(f"• 스캔 주기 설정 : {SCAN_INTERVAL_HOURS}시간 간격")
    print(f"• 텔레그램 연동  : {'설정됨 (ON)' if (TG_TOKEN and TG_CHAT_ID) else '미설정 (OFF)'}")
    print("==========================================")

def create_zotero_client():
    if not USER_ID or not API_KEY:
        log_error("🚨 ZOTERO_USER_ID 또는 ZOTERO_API_KEY 환경변수가 없습니다. 봇을 종료합니다.")
        return None
    return zotero.Zotero(USER_ID, LIBRARY_TYPE, API_KEY)

# ==========================================
# 8. 메인 실행 루프
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="Zotero Global CCTV - Smart Library Audit & Alert Bot")
    parser.add_argument("--once", action="store_true", help="1회 즉시 스캔 실행 후 종료합니다.")
    parser.add_argument("--dry-run", action="store_true", help="상태 저장 및 알림 발송 없이 스캔 테스트만 수행합니다.")
    parser.add_argument("--status", action="store_true", help="현재 로컬 상태 파일 요약 정보를 출력합니다.")
    parser.add_argument("--heal", action="store_true", help="미확인 폴더명 복구 및 상태 파일 정리를 즉시 수행합니다.")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    log_message(
        f"🌐 Zotero 전역 CCTV 스마트 스케줄러 v{VERSION} 가동 "
        f"(버전 동기화 & 전체 폴더 매핑 최적화 완료 | 주기: {SCAN_INTERVAL_HOURS}시간)"
    )

    zot = create_zotero_client()
    if zot is None:
        return

    if args.once or args.dry_run or args.heal:
        run_audit(zot, dry_run=args.dry_run, force_heal=args.heal)
        return

    # 시작 시 무조건 1회 스캔
    try:
        run_audit(zot)
    except Exception as e:
        log_error(f"⚠️ 초기 스캔 중 오류 발생 ({type(e).__name__}): {e}")

    # 주기적 스케줄 설정 (기본 6시간)
    schedule.every(SCAN_INTERVAL_HOURS).hours.do(run_audit, zot)

    while not STOP_REQUESTED:
        try:
            schedule.run_pending()
        except Exception as e:
            log_error(f"⚠️ 스케줄러 실행 중 예상치 못한 에러 ({type(e).__name__}): {e}")

        # 1초 단위로 쪼개어 대기함으로써 도커 컨테이너 종료/재시작 시 0.1초 만에 즉각 반응
        for _ in range(60):
            if STOP_REQUESTED:
                break
            time.sleep(1)

    log_message("👋 Zotero 전역 CCTV 봇이 안전하게 종료되었습니다.")

if __name__ == "__main__":
    main()
