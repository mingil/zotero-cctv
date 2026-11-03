# 🛡️ Zotero 전역 CCTV (`zotero-cctv`)

> 🌐 **Language**: [English](./README.md) | **한국어 가이드 (Korean)**

[![Docker Image CI](https://github.com/mingil/zotero-cctv/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/mingil/zotero-cctv/actions/workflows/docker-publish.yml)
[![Docker Hub](https://img.shields.io/docker/v/kimmingil/zotero-cctv?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/kimmingil/zotero-cctv)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)

> **수천~수만 편의 논문이 담긴 Zotero 라이브러리를 24시간 감시하는 스마트 보안·감사(Audit) 도커 봇입니다. 실수로 인한 논문 삭제나 휴지통 이동을 즉시 포착하여 원래 속해 있던 폴더 이름과 함께 텔레그램 푸시 알림을 보내줍니다.**

---

## ✨ 주요 특징 (v2.0 최적화 완료)

1. **⚡ 0.1초 초고속 버전 동기화 (`last_modified_version`)**
   - 매 스캔 시 Zotero 서버의 최신 버전 번호만 먼저 확인합니다.
   - 변경 사항이 없으면 전체 데이터를 다시 받지 않고 **0.1초 만에 스캔을 종료**하여 API 호출과 NAS 리소스를 아낍니다.
   - 변경 사항이 있을 때만 이전 버전 이후에 바뀐 항목(`since=last_version`)만 골라 초고속으로 가져옵니다.
2. **🚨 휴지통 이동 & 영구 삭제 이중 감지**
   - 논문이 완전히 삭제(`deleted`)되었을 때뿐만 아니라, 실수로 **휴지통으로 이동(`deleted: 1`)**된 순간에도 즉시 감지합니다.
   - 사라진 **논문 제목**과 **원래 들어있던 폴더(컬렉션) 이름**을 텔레그램으로 즉시 알려주어 언제든 복구할 수 있게 돕습니다.
3. **🗂️ 무제한 폴더(컬렉션) 매핑 및 자가 치유(Self-Healing) 엔진**
   - Zotero API의 기본 100개 폴더 조회 한계를 넘어 라이브러리 내 **모든 하위 폴더(`zot.everything(zot.collections())`)를 전수 매핑**합니다.
   - 과거 폴더 이름을 상태 파일에 함께 기억하여, 폴더와 논문이 동시에 삭제되더라도 `알수없는폴더` 대신 실제 폴더 이름을 정확히 알려줍니다.
   - 논문이 아닌 단순 메모(`note`), 첨부파일(`attachment`), 하이라이트(`annotation`)는 자동으로 걸러내어 `'제목 없음'` 오탐 알림을 방지합니다.
4. **🔄 모든 변경 이력 블랙박스 기록 (`zotero_audit_global.log`)**
   - **신규 논문 추가(`✨`)**, **폴더 간 이동(`🔄 IN / OUT`)**, **삭제/휴지통 이동(`🚨`)** 내역을 시간순으로 꼼꼼하게 기록하며, 로그 용량이 커지지 않도록 자동 회전(`5MB × 최대 3개`) 관리합니다.
5. **💾 전원 차단에도 안전한 원자적 저장 (Atomic Write) & 자동 재시도**
   - 임시 파일에 먼저 기록하고 디스크 동기화(`fsync`) 후 원본을 교체하므로, 시놀로지 NAS가 갑자기 재부팅되어도 상태 파일(`zotero_state_global.json`)이 절대 깨지지 않습니다.
   - 일시적인 인터넷/DNS 순단 발생 시 지수 백오프(5초 → 10초 → 20초)로 최대 3회 자동 재시도합니다.

---

## 🚀 1분 설치 가이드 (시놀로지 NAS / Docker Hub)

소스 코드를 직접 다운로드하거나 빌드할 필요 없이, **Docker Hub (`kimmingil/zotero-cctv:latest`)** 공식 이미지를 이용해 `compose.yaml`과 `.env` 파일 2개만으로 바로 실행할 수 있습니다.

### 1. `compose.yaml` 작성

```yaml
services:
  cctv-bot:
    image: kimmingil/zotero-cctv:latest
    container_name: zotero_cctv
    restart: unless-stopped
    init: true
    env_file:
      - .env
    environment:
      - TZ=Asia/Seoul
      - PYTHONUNBUFFERED=1
    volumes:
      - .:/app
    deploy:
      resources:
        limits:
          memory: 256M
```

### 2. `.env` 설정 파일 작성

```env
ZOTERO_USER_ID=당신의_Zotero_숫자_ID
ZOTERO_API_KEY=당신의_Zotero_API_키
TELEGRAM_BOT_TOKEN=텔레그램_봇_토큰
TELEGRAM_CHAT_ID=텔레그램_채팅방_ID

# 선택 옵션 (생략 시 아래 기본값으로 작동)
ZOTERO_LIBRARY_TYPE=user
SCAN_INTERVAL_HOURS=6
ALERT_ON_DELETE=true
ALERT_ON_ADD=false
ALERT_ON_MOVE=false
```

### 3. 컨테이너 실행

```bash
docker compose up -d
```

---

## 🛠️ 상태 확인 및 수동 명령어

컨테이너가 실행 중일 때 언제든 아래 명령어로 상태를 점검하거나 즉시 스캔을 실행할 수 있습니다:

```bash
# 1. 현재 감시 중인 논문 수, 폴더 수, 버전 상태 확인
docker exec -it zotero_cctv python cctv_bot.py --status

# 2. 기다리지 않고 지금 즉시 1회 스캔 실행
docker exec -it zotero_cctv python cctv_bot.py --once

# 3. 알림 발송이나 파일 저장 없이 연결 테스트만 수행 (Dry-Run)
docker exec -it zotero_cctv python cctv_bot.py --dry-run

# 4. 미확인 폴더명 복구 및 비-논문(메모/첨부파일) 자가 치유 실행
docker exec -it zotero_cctv python cctv_bot.py --heal
```

---

## 📜 라이선스

본 프로젝트는 [MIT License](./LICENSE)에 따라 누구나 자유롭게 사용, 수정 및 배포할 수 있습니다.
