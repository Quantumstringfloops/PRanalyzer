from pydantic import BaseModel

class ChangedFile(BaseModel):
    filename: str
    status:str
    updated: str