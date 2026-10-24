# Group Project Backend

그룹 프로젝트의 백엔드 애플리케이션입니다. Python 패키지와 의존성은
[uv](https://docs.astral.sh/uv/)로 관리합니다.

## 요구사항

- Python 3.13 이상
- uv

## 시작하기

저장소를 복제한 후 프로젝트 디렉터리에서 의존성을 설치합니다.

```bash
uv sync
```

이 명령은 `uv.lock`에 기록된 버전을 기준으로 의존성을 설치하고 로컬
가상환경인 `.venv`를 생성합니다.

애플리케이션을 실행합니다.

```bash
uv run group-project-backend
```

## 프로젝트 구조

```text
.
├── src/group_project_backend/  # 애플리케이션 소스 코드
├── pyproject.toml              # 프로젝트 및 의존성 설정
└── uv.lock                     # 잠긴 의존성 버전
```

`.venv`는 `uv sync` 실행 시 로컬에 생성되며 Git에는 포함하지 않습니다.
