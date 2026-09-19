from pydantic import BaseModel

class ChangedFile(BaseModel):
    filename: str
    status:str
    additions: int
    deletions: int
    patch: str | None = None


changed_file = ChangedFile(
    filename="/src/app.py",
    status= "updated",
    additions=5,
    deletions=2
)

print(changed_file.filename)