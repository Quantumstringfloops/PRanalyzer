import re 
from pydantic import BaseModel, ConfigDict

class ChangedFile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str

class Finding(BaseModel):
    rule: str
    filename: str
    code: str
    suggestion: str



RULES = [
    (
        "debug-print",
        re.compile(
            r"^\+(?!\+\+)(?P<code>\s*print\s*\(.*)$",
            re.MULTILINE,
        ),
        "Remove debugging print() calls or use structured logging.",
    ),
    (
        "unfinished-work",
        re.compile(
            r"^\+(?!\+\+)(?P<code>.*\b(?:TODO|FIXME)\b.*)$",
            re.MULTILINE | re.IGNORECASE,
        ),
        "Resolve or track this TODO/FIXME before merging.",
    ),
    (
        "bare-except",
        re.compile(
            r"^\+(?!\+\+)(?P<code>\s*except\s*:.*)$",
            re.MULTILINE,
        ),
        "Catch a specific exception instead of using bare except.",
    ),
]
