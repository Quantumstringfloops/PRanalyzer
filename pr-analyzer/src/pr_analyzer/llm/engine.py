import json
import os

from anthropic import Anthropic
from openai import OpenAI
from google import genai
from google.genai import types

import structlog
from pr_analyzer.models import Suggestion, PRSummary
from pr_analyzer.llm.prompts import build_prompts, SYSTEM_PROMPT

log = structlog.get_logger(__name__)

class LLMEngine:
    def __init__(self):
        provider=
    def analyze(): # can take any provider
    def call_openai(self, prompt:str) -> str:
        client = OpenAI()
        responses = client.responses.parse(
            model = "gpt-4o-mini",
            input=[
                {
                    "role"="system",
                    "content":SYSTEM_PROMPT
                },
                {
                    "role", "user",
                    "content": USER_PROMPT
                },
            ],
        )
    def 