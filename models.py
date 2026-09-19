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



## FIELD CLASS USAGE 

# class ChangedFile(BaseModel):
#     filename: str = Field(
#         min_length=1, MINIMUM LENGHT
#         description="Path of the changed file", DESCRIPTION
#     )
#     status: str = Field(
#         description="GitHub status of the file",
#     )
#     additions: int = Field(
#         default=0, MAKES FIELD OPTIONAL AND KEEPS DEAULT AS 0
#         ge=0, GREATER THAN OR EQUAL 
#         description="Number of added lines",
#     )
#     deletions: int = Field(
#         default=0,
#         ge=0, GREATER THAN OR EQUAL
#         description="Number of deleted lines",
#     )
#     patch: str | None = Field(
#         default=None,
#         description="Unified diff returned by GitHub",
#     )