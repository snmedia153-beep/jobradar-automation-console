# JobRadar Automation Console

JobRadar는 사람인 모바일 채용공고를 수집하고 운영 상태를 관리하기 위한 로컬 자동화 콘솔입니다. FastAPI, Streamlit, Redis Queue, Postgres, Playwright, Appium을 조합해 PC 브라우저 수집과 Android Emulator 기반 모바일 수집을 모두 다룰 수 있도록 구성했습니다.

## 주요 기능

- Streamlit 기반 운영 대시보드
- FastAPI Control Plane
- Redis 기반 Worker Queue
- Postgres 저장소
- Playwright 기반 PC 브라우저 수집
- Appium 기반 Android Emulator/USB 기기 수집
- Windows Host Agent를 통한 에뮬레이터 창 정렬, Appium 제어, ADB 상태 동기화
- CSV/JSON 내보내기와 알림 규칙 관리

## 구조

```text
Windows Host
├─ Android Emulator / USB Device
├─ Appium Server
├─ Host Agent
└─ Docker Desktop
   ├─ jobradar-api       FastAPI control plane
   ├─ jobradar-gui       Streamlit console
   ├─ jobradar-worker    Playwright/Appium workers
   ├─ postgres           job/result storage
   └─ redis              queue and worker events
```

Android Emulator와 Appium은 Windows 호스트에서 실행하고, Docker 컨테이너는 `host.docker.internal`을 통해 Host Agent/Appium에 연결합니다.

## 빠른 시작

```powershell
copy .env.example .env
notepad .env

docker compose build
docker compose up -d postgres redis
docker compose --profile init run --rm jobradar-init
docker compose up -d jobradar-api jobradar-gui
```

GUI는 아래 주소에서 확인할 수 있습니다.

```text
http://localhost:8501
```

API 문서는 아래 주소에서 확인할 수 있습니다.

```text
http://localhost:8000/docs
```

## 모바일 수집 실행 순서

Windows에서 Android SDK, Appium, uiautomator2 driver가 준비되어 있어야 합니다.

```powershell
npm install -g appium
appium driver install uiautomator2
```

Host Agent와 Appium 슬롯을 실행합니다.

```powershell
.\scripts\start_host_agent.ps1 -NewWindow
.\scripts\start_appium_5slots.ps1 -OnlyMissing
```

Worker까지 함께 실행하려면 다음 명령을 사용합니다.

```powershell
.\scripts\deploy_jobradar.ps1 -StartHostAgent -StartAppium -WithWorker
```

## 자주 쓰는 명령

```powershell
# 상태 확인
docker compose ps

docker compose exec jobradar-api python -m jobradar.cli db-check
docker compose exec jobradar-api python -m jobradar.cli redis-check
docker compose exec jobradar-api python -m jobradar.cli device-list
docker compose exec jobradar-api python -m jobradar.cli appium-health

# 수집 작업 등록
docker compose exec jobradar-api python -m jobradar.cli queue-collection --mode appium

# Worker 실행
docker compose --profile worker up -d jobradar-appium-worker
```

## 저장소 공개 전제

이 저장소에는 실제 `.env`, 실행 로그, 수집 결과, 백업 파일을 포함하지 않습니다. 실행하려면 `.env.example`을 `.env`로 복사한 뒤 본인 PC의 Android SDK 경로와 DB 비밀번호를 입력하세요.

## 보안 주의

이 프로젝트의 API와 Host Agent는 로컬 개발 또는 내부망 운영을 전제로 합니다. 인증 레이어가 필요한 외부 공개 환경에서는 Reverse Proxy 인증, 방화벽, API Token 검증을 추가해야 합니다. 수집 대상 사이트의 이용약관과 트래픽 정책을 준수해서 사용하세요.
