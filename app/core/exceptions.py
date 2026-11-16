"""공통 도메인 예외. 기능별 예외는 각 기능 service.py에 둔다."""


class AppError(Exception):
    """애플리케이션 예외의 기준 클래스."""
