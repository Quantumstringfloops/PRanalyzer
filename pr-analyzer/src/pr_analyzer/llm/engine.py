import json
import os
import httpx
import anthropic
import openai
import ollama
from anthropic import Anthropic
from openai import OpenAI
from google import genai
from google.genai import types
from ollama import Client

import structlog
from pr_analyzer.models import Suggestion, PRSummary
from pr_analyzer.llm.prompts import build_prompt, SYSTEM_PROMPT
from pr_analyzer.config import settings
log = structlog.get_logger(__name__)

class LLMEngine:

    def analyze(self, pr:PRSummary, existing_findings:list[Suggestion])-> list[Suggestion]:
         # can take any provider
        provider = settings.llm_provider

        prompt = build_prompt(pr, existing_findings, settings.max_diff_lines)

        if provider == "openai":
            raw = self.call_openai(prompt)
        elif provider == "anthropic":
            raw = self.call_anthropic(prompt)
        elif provider == "ollama":
            raw = self.call_ollama(prompt)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        parsed = self.parse_reponse(raw)
        validated = [ item for item in parsed if self.validate_suggestion(item)]
        confidence = self.filter_confidence(validated)
        duplicates = self.filter_duplicates(confidence,existing_findings)
        suggestions = self.to_suggestions(duplicates)

        
        log.info("Details : ", 
                 provider= provider, 
                 model=settings.llm_model,
                 count = len(suggestions)
                
            )
        return suggestions
    
    def call_openai(self, prompt:str) -> str:
        client = OpenAI(api_key=settings.openai_api_key)
        try:
            responses = client.chat.completions.create(
                model = settings.llm_model,
                response_format = {"type": "json_object"},
                messages =[
                    {"role":"system", "content": SYSTEM_PROMPT},
                    {"role":"user", "content": prompt}
                ]
            )
        except openai.APIConnectionError:
            log.warning("API connection error")
            return "[]"
        except openai.APITimeoutError:
            log.warning("API timeout")
            return "[]"
        except Exception as e:
            log.warning("Error:", error=e)

        return responses.choices[0].message.content

    
    def call_anthropic(self, prompt:str) -> str:
        client = anthropic.Anthropic()
        try:

            responses = client.messages.create(
                model= settings.llm_model,
                max_tokens= 1024,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
        except anthropic.APITimeoutError:
            log.warning("API timeout error")
            return "[]"
        except anthropic.APIConnectionError:
            log.warning("API connection error")
            return "[]"
        except Exception as e:
            log.warning("Error:", error=e)
        return responses.content[0].text

    
    def call_ollama(self, prompt:str, ) -> str:
        url = "http://localhost:11434/api/chat"
        payload = {
            "model" : settings.llm_model,
            "messages":
            [{"role":"system","content":SYSTEM_PROMPT},{"role":"user", "content":prompt},],
        }
        try:
            response =""
            with httpx.Client(timeout=None) as client:
                with client.stream("POST", url, json=payload) as response:
                    for line in response.iter_lines():
                        if line.strip():
                            data = json.loads(line)
                            if "message" in data and "content" in data["message"]:
                                response += data["message"]["content"]
        except httpx.TimeoutException:
            log.warning("API timeout error")
            return "[]"
        except httpx.ConnectError:
            log.warning("API connection error")
            return "[]"
        except httpx.ConnectTimeout:
            log.warning("Connection refused")
            return "[]"
        except Exception as e:
            log.warning("Error:", error=e)
            return "[]"

        return response


    def parse_reponse(self,raw:dict) -> list[dict]:
        try:
            data = json.loads(raw)
            if isinstance(data,dict):
                data = data.get("suggestions", list(data.values())[0])
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
        REQUIRED = {"filename", "severity", "category", "title", "description", "confidence"}
        return REQUIRED.issubset(set(item.keys()))

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
        keep = []
        for item in items:
            is_duplicate = any(
                item["filename"] == e.filename and
                item["line"] == e.line and
                item["category"] == e.category
                for e in existing
            )
            if not is_duplicate:
                keep.append(item)
        return keep


    def to_suggestions(self, items: list[dict])-> list[Suggestion]:
        suggestions = []
        for item in items:
            file_name = item["filename"]
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
