import argparse
import json
import os
import sys
from typing import Literal

from anthropic import Anthropic
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError


class Suggestion(BaseModel):
    category: str
    severity: Literal["low", "medium", "high"]
    title: str
    description: str
    file: str
    line_start: int | None
    confidence: float = Field(ge=0, le=1)


SYSTEM_PROMPT = """
You are a pull-request code reviewer.

Return only a valid JSON array. Do not use Markdown code fences,
headings, explanations, or text outside the array.

Each array element must contain exactly these fields:
- category: string
- severity: "low", "medium", or "high"
- title: string
- description: string
- file: string
- line_start: integer or null
- confidence: number between 0 and 1

If there are no useful suggestions, return [].
Do not report formatting preferences unless they affect correctness,
security, maintainability, or reliability.
""".strip()


DIFF_LINES = [
    "@@ -10,4 +10,18 @@ def process_users(users):",
    "-    return [process(user) for user in users]",
    "+    results = []",
    '+    password = "admin123"',
    '+    print("Processing users")',
    "+    try:",
    "+        for user in users:",
    "+            if user.is_active:",
    "+                if user.has_permission:",
    "+                    results.append(process(user))",
    "+                else:",
    "+                    continue",
    "+    except:",
    "+        pass",
    "+    if len(results) == 0:",
    "+        return []",
    "+    # TODO: add proper error handling",
    "+    return results",
    " ",
    " def cleanup():",
]

DIFF = "\n".join(DIFF_LINES)
assert len(DIFF.splitlines()) == 20


USER_MESSAGE = f"""
Review this 20-line unified diff and return useful code-review
suggestions using the required JSON-array format.

File: src/users.py

Diff:
{DIFF}
""".strip()


def call_openai() -> str:
    client = OpenAI()

    response = client.responses.create(
        model=os.environ["OPENAI_MODEL"],
        instructions=SYSTEM_PROMPT,
        input=USER_MESSAGE,

        # Intentionally no JSON mode or JSON schema.
    )

    return response.output_text


def call_anthropic() -> str:
    client = Anthropic()

    response = client.messages.create(
        model=os.environ["ANTHROPIC_MODEL"],
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_MESSAGE,
            }
        ],

        # Intentionally no output_config JSON schema.
    )

    return "".join(
        block.text
        for block in response.content
        if block.type == "text"
    )


def parse_and_validate(raw_content: str) -> list[Suggestion]:
    # Manual JSON parsing, as required by the experiment.
    parsed = json.loads(raw_content)

    if not isinstance(parsed, list):
        raise TypeError(
            "Expected a top-level JSON array, "
            f"received {type(parsed).__name__}"
        )

    if not parsed:
        print("\nThe provider returned an empty array.")
        return []

    # Explicitly validate the first suggestion.
    first_suggestion = Suggestion.model_validate(parsed[0])

    print("\nFirst validated suggestion:")
    print(first_suggestion.model_dump_json(indent=2))

    # Validate every remaining suggestion.
    validated = [
        Suggestion.model_validate(item)
        for item in parsed
    ]

    return validated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "provider",
        choices=["openai", "anthropic"],
    )
    arguments = parser.parse_args()

    if arguments.provider == "openai":
        raw_content = call_openai()
    else:
        raw_content = call_anthropic()

    print("----- RAW RESPONSE START -----")

    # Writes the content without modifying or formatting it.
    sys.stdout.write(raw_content)

    print("\n----- RAW RESPONSE END -----")

    try:
        suggestions = parse_and_validate(raw_content)
    except json.JSONDecodeError as error:
        print("\nJSON parsing failed:")
        print(error)
        print(
            "The provider returned text that was not valid JSON. "
            "This is expected to be possible because JSON mode "
            "was intentionally omitted."
        )
        return
    except (ValidationError, TypeError) as error:
        print("\nSchema validation failed:")
        print(error)
        return

    accepted = [
        suggestion
        for suggestion in suggestions
        if suggestion.confidence >= 0.6
    ]

    print("\n----- ACCEPTED SUGGESTIONS -----")

    for suggestion in accepted:
        print(suggestion.model_dump_json(indent=2))

    filtered_count = len(suggestions) - len(accepted)

    print(f"\nAccepted: {len(accepted)}")
    print(f"Filtered below 0.6: {filtered_count}")


if __name__ == "__main__":
    main()