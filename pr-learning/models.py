from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

# class ChangedFile(BaseModel):
#     filename: str
#     status:str
#     additions: int
#     deletions: int
#     patch: str | None = None


# changed_file = ChangedFile(
#     filename="/src/app.py",
#     status= "updated",
#     additions=5,
#     deletions=2
# )

# print(changed_file.filename)



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

# MODEL VALIDATE

# file_data = {
#     "filename": "src/main.py",
#     "status": "modified",
#     "additions": "5",
#     "deletions": 2,
#     "patch": "@@ -1,2 +1,5 @@",
# } 

# file_check = ChangedFile.model_validate(file_data)
# print(file_check)

# # MODEL DUMP 
# # Creates a python dictinary

# result = ChangedFile.model_dump(changed_file)
# print(result)

#FIELD VALIDATOR CHECKS OR TRANFORMS A CERTAIN FIELD
class ChangedFile(BaseModel):
    filename: str = Field(min_length=1)
    status: str
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    patch: str | None = None

    #FIELD VALIDATOR CHECKS OR TRANFORMS A CERTAIN FIELD
    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized_value = value.strip().lower()

        allowed_statuses = {
            "added",
            "modified",
            "removed",
            "renamed",
        }

        if normalized_value not in allowed_statuses:
            raise ValueError(
                f"Unsupported file status: {value}"
            )

        return normalized_value

    # Validation for multiple fields especially for objects 
    # Example is to check if file is changd by checking addition and deletion atleast 1 or more
    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        if self.additions == 0 and self.deletions == 0:
            raise ValueError(
                "Changed file has no additions or deletions"
            )

        return self

file = ChangedFile(
    filename="main.py",
    status="  MODIFIED  ",
    additions=2,
    deletions=1,
)

print(file.status)