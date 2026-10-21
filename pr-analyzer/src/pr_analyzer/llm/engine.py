import json
import os
import httpx
from anthropic import Anthropic
from openai import OpenAI
from google import genai
from google.genai import types
from ollama import Client

import structlog
from pr_analyzer.models import Suggestion, PRSummary
from pr_analyzer.llm.prompts import build_prompts, SYSTEM_PROMPT
from pr_analyzer.config import settings
log = structlog.get_logger(__name__)

class LLMEngine:

    def analyze(self, pr:PRSummary, existing_findings:list[Suggestion]-> list[Suggestion]):
         # can take any provider
        provider = settings.llm_provider
        if provider == "openai":
            raw = self._call_openai(prompt)
        elif provider == "anthropic":
            raw = self._call_anthropic(prompt)
        elif provider == "ollama":
            raw = self._call_ollama(prompt)
        else:
            raise ValueError(f"Unknown provider: {provider}")



    
    def call_openai(self, prompt:str) -> str:
        client = OpenAI(settings.openai_api_key)
        responses = client.responses.parse(
            model = settings.llm_model,
            response_format = {"type": "json_object"},
            messages =[
                {"role":"system", "content": SYSTEM_PROMPT},
                {"role":"user", "content": prompt}
            ]
        )

        return responses.choices[0].message.content
    # TODO 
    def call_anthropic(self, prompt:str) -> str:
        client = Anthropic(settings.anthropic_api_key)

        responses = client.messages.create(
            model= settings.llm_model,
            max_tokens= 1024,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        return responses.content[0].text

    
    def call_ollama(self, prompt:str, ) -> str:
        url = "http://localhost:11434/api/chat"
        payload = {
            "model" : settings.llm_model,
            "messages":
            [{"role":"system","content":SYSTEM_PROMPT},{"role":"user", "content":prompt},],
        }

        with httpx.Client(timeout=None) as client:
            with client.stream("POST", url, json=payload) as response:
                for line in response.iter_lines():
                    if line.strip():
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            print(data["message"]["content"], end="", flush=True)
    def parse_reponse(self,raw:dict) -> list[dict]:
