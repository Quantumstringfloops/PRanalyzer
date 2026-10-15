from datetime import datetime


class PRAnalyzerError(Exception):
    pass


class InvalidTokenError(PRAnalyzerError):
    pass


class PrivateRepoError(PRAnalyzerError):
    pass


class PRNotFoundError(PRAnalyzerError):
    pass


class RepositoryNotFoundError(PRAnalyzerError):
    pass


class RateLimitError(PRAnalyzerError):
    def __init__(self, reset_time: datetime):
        self.reset_time = reset_time
        super().__init__(f"Rate limit exceeded. Resets at {reset_time.strftime('%H:%M:%S')}")