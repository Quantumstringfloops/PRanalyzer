import json
import os

from anthropic import Anthropic
from openai import OpenAI
from google import genai
from google.genai import types

import structlog
from pr_analyzer.models import Suggestion, FileChange
from pr_analyzer.llm.prompts import build_prompts

log = structlog.get_logger(__name__)