class AppError(Exception):
    """서비스 전역에서 던지는 업무 오류. 라우터의 예외 처리기가 공통 실패 응답으로 바꾼다."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        """오류를 만든다.

        파라미터:
            code: 클라이언트가 분기에 쓰는 오류 코드(예: FILE_NOT_FOUND)
            message: 사용자에게 보여줄 한국어 메시지
            status_code: 응답 HTTP 상태 코드(기본 400)
        """
        # str(exc)와 로그에도 메시지가 보이도록 부모에 넘긴다
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
