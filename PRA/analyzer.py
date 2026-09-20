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
