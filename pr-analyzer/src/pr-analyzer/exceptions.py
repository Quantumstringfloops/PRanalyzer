class PRAnalyzerError(Exception):
    """Base class for all pr-analyzer errors."""
    pass

class InvalidTokenError(PRAnalyzerError):
    pass

class PrivateRepoError(PRAnalyzerError):
    pass

class PRNotFoundError(PRAnalyzerError):
    pass

class RateLimitError(PRAnalyzerError):
    pass

class RepositoryNotFoundError(PRAnalyzerError):
    pass