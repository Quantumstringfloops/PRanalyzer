from datetime import datetime


class PRAnalyzerError(Exception):
    pass


class InvalidPRURLError(PRAnalyzerError, ValueError):
    # also a ValueError, so code that validates URLs with `except ValueError` keeps working
    pass


class ConfigError(PRAnalyzerError):
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


class LLMError(PRAnalyzerError):
    # the LLM provider could not be reached or returned something unusable
    pass
