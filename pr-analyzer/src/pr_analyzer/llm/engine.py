import json
import os
import httpx
import anthropic
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
        client = anthropic.Anthropic()

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
        try:
            data = json.loads(raw)
            if isinstance(data,dict):
                data = data.get("suggestions", list(data.values()[0]))
            return data if isinstance(data, list) else[]
        
        except json.JSONDecodeError:
            pass

        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1:
            try:
                return json.loads(raw[start:end+1])
            except json.JSONDecodeError:
                pass

        log.warning("failed to parse LLM response")
        return []


    def validate_suggestion(self, item: dict)-> bool:
        list_check = list(item)
        for item in list_check:
            if item.lower() in ["filename", "severity", "category", "title", "description", "confidence"]:
                log.info("Valid output")
                return True
            else:
                log.warn("Missing field(s)")
                return False

    def filter_confidence(self, items: list[dict]) -> list[dict]:
        scores = []
        dropped = []
        for item in items:
            if item["confidence"] > settings.llm_confidence_threshold:
                scores.append(item)
            else:
                dropped.append(item)

            log.info("Few items were dropped due to low confidence scores",  dropped_items = dropped)
        return scores

    def filter_duplicates(self, items: list[dict], existing: list[Suggestion])-> list[dict]:
        item = []
        for item in items:
            for file in existing:
                if item["filename"] == file.filename and item["line"] == file.line and item["category"] == file.category and item["severity"] == file.severity:
                    items.remove(item)


    def to_suggestions(self, items: list[dict])-> list[Suggestion]:
        suggestions = []
        for item in items:
            file_list = item["filename"].strip("/").split("/")
            file_name = file_list[-1]
            suggestions.append(Suggestion(
                filename=file_name,
                severity=item["severity"],
                category=item["category"],
                title=item["title"],
                description=item["description"],
                line=item["line"],
                source = "llm"
            ))

        return suggestions
