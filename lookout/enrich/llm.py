"""
LLM client wrapper for provider-agnostic LLM access.

Currently implements Anthropic Claude, but structured to allow
adding other providers (OpenAI, Google) in the future.
"""

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """
        Generate a completion from the LLM.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            The generated text response.
        """
        pass

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        schema: type[T],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> T:
        """
        Generate a JSON response matching a Pydantic schema.

        Args:
            prompt: The user prompt.
            schema: Pydantic model class for validation.
            system: Optional system prompt.
            max_tokens: Maximum tokens in response.

        Returns:
            Validated Pydantic model instance.
        """
        pass


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        """
        Initialize the Anthropic provider.

        Args:
            api_key: Anthropic API key. If not provided, reads from
                    ANTHROPIC_API_KEY environment variable.
            model: Model name to use.
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment "
                "variable or pass api_key parameter."
            )
        self.model = model
        self._client = None

    async def _get_client(self) -> Any:
        """Get or create the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
    )
    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Generate a completion from Claude."""
        client = await self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        if system:
            kwargs["system"] = system

        if temperature > 0:
            kwargs["temperature"] = temperature

        response = await client.messages.create(**kwargs)
        return response.content[0].text

    async def complete_json(
        self,
        prompt: str,
        schema: type[T],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> T:
        """Generate a JSON response matching a Pydantic schema."""
        # Add JSON instruction to system prompt
        json_system = (system or "") + (
            "\n\nYou must respond with valid JSON that matches the requested schema. "
            "Do not include any text before or after the JSON object. "
            "Do not use markdown code blocks."
        )

        # Add schema to prompt
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        json_prompt = f"{prompt}\n\nRespond with JSON matching this schema:\n{schema_json}"

        # First attempt
        response = await self.complete(json_prompt, json_system, max_tokens)

        # Try to parse JSON
        try:
            parsed = self._extract_and_parse_json(response)
            return schema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as e:
            parse_error = e
            logger.warning(f"First JSON parse attempt failed: {e}. Retrying...")

        # Retry with fix instruction
        fix_prompt = (
            f"The previous response was not valid JSON or didn't match the schema.\n"
            f"Error: {parse_error}\n\n"
            f"Original response:\n{response}\n\n"
            f"Please fix the JSON to match this schema:\n{schema_json}\n\n"
            f"Respond ONLY with the corrected JSON, no other text."
        )

        response = await self.complete(fix_prompt, json_system, max_tokens)

        # Parse again
        parsed = self._extract_and_parse_json(response)
        return schema.model_validate(parsed)

    def _extract_and_parse_json(self, text: str) -> dict[str, Any]:
        """Extract JSON from text that might have surrounding content."""
        text = text.strip()

        # Remove markdown code blocks if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())

        raise json.JSONDecodeError("No valid JSON found", text, 0)


class LLMClient:
    """
    High-level LLM client with prompt template support.

    Provides a clean interface for LLM operations with:
    - Provider abstraction
    - Prompt template loading
    - JSON schema validation
    - Automatic retries
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        """
        Initialize the LLM client.

        Args:
            provider: LLM provider to use. Defaults to Anthropic.
            prompts_dir: Directory containing prompt templates.
        """
        self.provider = provider or AnthropicProvider()
        self.prompts_dir = prompts_dir or (Path(__file__).parent / "prompts")
        self._prompt_cache: dict[str, str] = {}

    def load_prompt(self, name: str) -> str:
        """
        Load a prompt template by name.

        Args:
            name: Prompt name (without .prompt extension).

        Returns:
            The prompt template text.
        """
        if name in self._prompt_cache:
            return self._prompt_cache[name]

        prompt_path = self.prompts_dir / f"{name}.prompt"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {prompt_path}")

        with open(prompt_path) as f:
            prompt = f.read()

        self._prompt_cache[name] = prompt
        return prompt

    async def extract_facts(
        self,
        source_text: dict[str, Any],
        raw_facts: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Use LLM to structure extracted facts.

        Args:
            source_text: Raw extracted source text.
            raw_facts: Deterministically extracted facts.

        Returns:
            Structured facts dictionary.
        """
        prompt_template = self.load_prompt("extract_facts")

        prompt = prompt_template.format(
            source_text=json.dumps(source_text, indent=2),
            raw_facts=json.dumps(raw_facts, indent=2),
        )

        system = (
            "You are a product data extraction assistant. Your task is to "
            "structure product information from web page content. "
            "IMPORTANT: You must NEVER invent or fabricate information. "
            "If information is not present in the source, leave the field empty "
            "or return an empty list. Always cite evidence from the source text."
        )

        response = await self.provider.complete(prompt, system)

        # Parse response as JSON
        try:
            return self.provider._extract_and_parse_json(response)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM facts response as JSON")
            return raw_facts

    async def generate_body_html(
        self,
        facts: dict[str, Any],
        handle: str,
        vendor: str,
    ) -> str:
        """
        Generate Shopify Body HTML from extracted facts.

        Args:
            facts: Extracted product facts.
            handle: Product handle.
            vendor: Vendor name.

        Returns:
            Generated HTML body.
        """
        prompt_template = self.load_prompt("generate_body_html")

        prompt = prompt_template.format(
            facts=json.dumps(facts, indent=2),
            handle=handle,
            vendor=vendor,
        )

        system = (
            "You write product descriptions for an independent outdoor gear shop. "
            "Write like a knowledgeable shop employee — direct, specific, human. "
            "ONLY use information from the provided facts. Never invent specs or claims. "
            "Vary your sentence structure. Avoid AI-writing patterns: don't start "
            "every sentence the same way, don't use filler words like 'boasts', "
            "'delivers', 'ensures', 'innovative', 'exceptional', 'utilize'. "
            "Be specific (use real numbers, materials, tech names) not vague."
        )

        return await self.provider.complete(prompt, system)

    async def select_variant_images(
        self,
        facts: dict[str, Any],
        available_images: list[dict[str, Any]],
    ) -> dict[str, str]:
        """
        Select variant images based on extracted data.

        Args:
            facts: Extracted product facts.
            available_images: List of available image info.

        Returns:
            Mapping of variant option value to image URL.
        """
        prompt_template = self.load_prompt("select_variant_images")

        prompt = prompt_template.format(
            facts=json.dumps(facts, indent=2),
            available_images=json.dumps(available_images, indent=2),
        )

        system = (
            "You are a product data assistant. Your task is to match variant "
            "options (like colors) to their corresponding product images. "
            "IMPORTANT: Only make assignments when there is clear evidence. "
            "If you cannot confidently match a color to an image, skip it. "
            "It's better to have missing assignments than wrong ones."
        )

        response = await self.provider.complete(prompt, system)

        try:
            return self.provider._extract_and_parse_json(response)
        except json.JSONDecodeError:
            logger.warning("Failed to parse variant image response as JSON")
            return {}


def get_llm_client(
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
    prompts_dir: Path | None = None,
) -> LLMClient:
    """
    Create an LLM client with default configuration.

    Args:
        api_key: Optional API key (uses env var if not provided).
        model: Model name to use.
        prompts_dir: Optional prompts directory.

    Returns:
        Configured LLMClient instance.
    """
    provider = AnthropicProvider(api_key=api_key, model=model)
    return LLMClient(provider=provider, prompts_dir=prompts_dir)
