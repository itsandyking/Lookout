"""
LLM client wrapper for provider-agnostic LLM access.

Currently implements Anthropic Claude with native structured output
via tool use for JSON responses.  Also provides OllamaVisionClient
for local vision-based variant image matching via Gemma 3 4B.
"""

import asyncio
import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
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
        """Generate a text completion."""
        pass

    @abstractmethod
    async def complete_structured(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        tool_name: str = "structured_output",
        tool_description: str = "Output structured data",
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Generate a structured JSON response matching a schema.

        Uses the provider's native structured output mechanism
        (e.g., tool use for Anthropic) instead of text parsing.

        Args:
            prompt: The user prompt.
            output_schema: JSON Schema dict for the response.
            tool_name: Name for the structured output tool.
            tool_description: Description of what the output represents.
            system: Optional system prompt.
            max_tokens: Maximum tokens in response.

        Returns:
            Parsed dict matching the schema.
        """
        pass


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider with native structured output."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
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
        """Generate a text completion from Claude."""
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
    )
    async def complete_structured(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        tool_name: str = "structured_output",
        tool_description: str = "Output structured data",
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Generate structured output using Claude's tool use.

        Defines a single tool with the desired schema and forces Claude
        to call it, producing guaranteed valid JSON without regex parsing.
        """
        client = await self._get_client()

        tool = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": output_schema,
        }

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
        }

        if system:
            kwargs["system"] = system

        response = await client.messages.create(**kwargs)

        # Extract the tool use result
        for block in response.content:
            if block.type == "tool_use":
                return block.input

        # Shouldn't reach here with forced tool_choice, but fallback
        logger.warning("No tool_use block in response, falling back to text parsing")
        for block in response.content:
            if block.type == "text":
                return self._extract_and_parse_json(block.text)

        return {}

    @staticmethod
    def _extract_and_parse_json(text: str) -> dict[str, Any]:
        """Fallback: extract JSON from text (used only if tool_use fails)."""
        import re

        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())

        raise json.JSONDecodeError("No valid JSON found", text, 0)


class ClaudeCLIProvider(LLMProvider):
    """LLM provider using `claude --print` CLI. Authenticates via Max subscription."""

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        import shutil
        self.model = model
        self.claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")

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
        import asyncio
        cmd = [
            self.claude_bin, "--print",
            "--model", self.model,
            "--max-turns", "1",
        ]
        if system:
            cmd += ["--system-prompt", system]

        # Don't pass ANTHROPIC_API_KEY to CLI — let it use its own auth (Max subscription)
        import os
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=300,
        )

        if proc.returncode != 0:
            err = stderr.decode().strip()
            out = stdout.decode().strip()[:200]
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): stderr={err!r} stdout={out!r}")

        return stdout.decode().strip()

    async def complete_structured(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        tool_name: str = "structured_output",
        tool_description: str = "Output structured data",
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Generate structured output via CLI by requesting JSON."""
        schema_hint = json.dumps(output_schema, indent=2)
        structured_prompt = (
            f"{prompt}\n\n"
            f"Respond with ONLY valid JSON matching this schema:\n"
            f"```json\n{schema_hint}\n```\n"
            f"No explanation, no markdown, just the JSON object."
        )

        structured_system = system or ""
        structured_system += "\nYou must respond with only valid JSON. No other text."

        text = await self.complete(structured_prompt, system=structured_system.strip(), max_tokens=max_tokens)
        return AnthropicProvider._extract_and_parse_json(text)


def _create_default_provider() -> LLMProvider:
    """Create the best available LLM provider.

    Prefers claude CLI (uses Max subscription), falls back to SDK.
    """
    import shutil
    claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")

    # Check if claude CLI is available
    if Path(claude_bin).exists():
        logger.info("Using claude CLI provider (Max subscription)")
        return ClaudeCLIProvider()

    # Fall back to SDK
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        logger.info("Using Anthropic SDK provider")
        return AnthropicProvider(api_key=api_key)

    raise ValueError(
        "No LLM provider available. Install claude CLI or set ANTHROPIC_API_KEY."
    )


class OllamaVisionClient:
    """Local vision model client for image color identification via Ollama.

    Uses a menu-based approach: given a list of variant color names,
    the model picks which one best matches each image rather than
    guessing a generic color name.
    """

    def __init__(
        self,
        model: str = "vision",
        base_url: str = "http://localhost:11434",
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    async def _post_vision(self, payload: dict) -> str:
        """Send a vision request to Ollama and return the response text."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            return result.get("response", "").strip().strip(".")

    @staticmethod
    def _describe_color_option(color: str) -> str:
        """Build a menu line for a color, expanding slash names.

        'Purple Ink/Purple Dusk/Cheddar' → '- Purple Ink/Purple Dusk/Cheddar (multi-color: purple, purple, cheddar)'
        'Black' → '- Black'
        """
        if "/" not in color:
            return f"- {color}"
        parts = [p.strip() for p in color.split("/")]
        # Extract the last word from each part as the core color
        core_colors = [p.split()[-1].lower() for p in parts]
        return f"- {color}  (multi-color: {', '.join(core_colors)})"

    def _build_prompt(
        self,
        color_options: list[str],
        image_url: str = "",
    ) -> str:
        """Build the vision prompt with color menu and URL hint."""
        options_list = "\n".join(
            self._describe_color_option(c) for c in color_options
        )

        url_hint = ""
        if image_url:
            from urllib.parse import unquote, urlparse
            path = unquote(urlparse(image_url).path)
            url_hint = f"\nImage URL path: {path}"

        return (
            f"This is a product photo. Which ONE of these color names "
            f"matches the product shown?\n\n"
            f"Color options:\n{options_list}\n"
            f"{url_hint}\n\n"
            f"Rules:\n"
            f"- Look at the ACTUAL COLOR of the product in the image\n"
            f"- Reply with the EXACT color name from the list, nothing else\n"
            f"- Only match if you are confident — if unsure, reply NONE\n"
            f"- Some options are multi-color (shown with /). If the product "
            f"has multiple colors that match those parts, pick that option\n"
            f"- Reply NONE if: this is a lifestyle/action photo, a size "
            f"chart, a detail closeup, or the product color isn't clearly "
            f"visible\n"
            f"- Reply NONE if no option is a good match for what you see"
        )

    async def match_image_to_color(
        self,
        image_data: bytes,
        color_options: list[str],
        image_url: str = "",
    ) -> str | None:
        """Ask the vision model which color option best matches this image.

        Args:
            image_data: Raw image bytes.
            color_options: List of variant color names to choose from.
            image_url: URL of the image (included as extra context).

        Returns:
            The exact color name from color_options, or None if no match.
        """
        b64 = base64.b64encode(image_data).decode()

        payload = {
            "model": self.model,
            "prompt": self._build_prompt(color_options, image_url),
            "images": [b64],
            "stream": False,
            "think": False,
            "options": {"num_predict": 30, "temperature": 0.1},
        }

        raw = await self._post_vision(payload)

        if not raw or raw.upper() == "NONE":
            return None

        # Exact match (case-insensitive) against the options
        raw_lower = raw.lower()
        for option in color_options:
            if option.lower() == raw_lower:
                return option

        # Partial match — model might have added/dropped words
        for option in color_options:
            if option.lower() in raw_lower or raw_lower in option.lower():
                return option

        logger.debug("Vision returned '%s' which didn't match any option", raw)
        return None

    async def _identify_color_freeform(
        self,
        image_data: bytes,
        image_url: str = "",
    ) -> str | None:
        """Ask the model to freely describe the product color (pass 2).

        Returns a short color description like 'dark purple and yellow',
        or None on failure.
        """
        b64 = base64.b64encode(image_data).decode()

        url_hint = ""
        if image_url:
            from urllib.parse import unquote, urlparse
            path = unquote(urlparse(image_url).path)
            url_hint = f"\nImage URL path: {path}"

        payload = {
            "model": self.model,
            "prompt": (
                f"What are the main colors of this product? "
                f"List the 1-3 dominant colors you see, separated by commas. "
                f"Be specific (e.g. 'dark olive green' not just 'green'). "
                f"If this isn't a clear product photo, reply NONE."
                f"{url_hint}"
            ),
            "images": [b64],
            "stream": False,
            "think": False,
            "options": {"num_predict": 30, "temperature": 0.1},
        }

        raw = await self._post_vision(payload)
        if not raw or raw.upper() == "NONE":
            return None
        return raw

    @staticmethod
    def _fuzzy_match_freeform(
        description: str,
        remaining_colors: list[str],
    ) -> str | None:
        """Match a free-form color description to remaining options.

        Uses token overlap: if the model says 'dark purple and yellow'
        and an option is 'Purple Ink/Purple Dusk/Cheddar', the overlap
        on 'purple' scores a match.
        """
        desc_tokens = {t.lower().strip(",.") for t in description.split()}
        # Remove noise words
        noise = {"and", "with", "the", "a", "an", "dark", "light", "bright",
                 "deep", "pale", "matte", "product", "is", "color", "colored"}
        desc_tokens -= noise

        best_option = None
        best_score = 0

        for option in remaining_colors:
            # Tokenize the option, including slash-separated parts
            option_expanded = option.replace("/", " ").replace("-", " ")
            option_tokens = {t.lower().strip() for t in option_expanded.split()}
            option_tokens -= noise

            overlap = desc_tokens & option_tokens
            if len(overlap) > best_score:
                best_score = len(overlap)
                best_option = option

        # Require at least 1 meaningful token overlap
        if best_score >= 1:
            return best_option
        return None

    async def match_images_batch(
        self,
        images: list[tuple[str, bytes]],
        color_options: list[str],
    ) -> dict[str, str]:
        """Match a batch of images to color options using two passes.

        Pass 1: Menu-based — model picks from the exact color names.
        Pass 2: Free-form — for unmatched images, ask the model to describe
                the color, then fuzzy-match back to remaining options.

        Each color can only be assigned once (first image wins).

        Args:
            images: List of (image_url, image_bytes) tuples.
            color_options: List of variant color names to choose from.

        Returns:
            Dict mapping color name → image URL.
        """
        mapping: dict[str, str] = {}
        remaining_colors = list(color_options)
        unmatched_images: list[tuple[str, bytes]] = []

        # Pass 1: Menu-based matching
        for url, data in images:
            if not remaining_colors:
                break
            try:
                matched = await self.match_image_to_color(
                    data, remaining_colors, image_url=url,
                )
                if matched:
                    mapping[matched] = url
                    remaining_colors.remove(matched)
                    logger.debug("Vision pass 1: %s → %s", url, matched)
                else:
                    unmatched_images.append((url, data))
                    logger.debug("Vision pass 1: %s → no match", url)
            except Exception as e:
                logger.warning("Vision pass 1 failed for %s: %s", url, e)
                unmatched_images.append((url, data))

        # Pass 2: Free-form identification for remaining colors
        if remaining_colors and unmatched_images:
            logger.info(
                "Vision pass 2: %d colors remain, %d images to retry",
                len(remaining_colors), len(unmatched_images),
            )
            used_urls: set[str] = set(mapping.values())

            for url, data in unmatched_images:
                if not remaining_colors or url in used_urls:
                    continue
                try:
                    description = await self._identify_color_freeform(data, image_url=url)
                    if not description:
                        continue
                    matched = self._fuzzy_match_freeform(description, remaining_colors)
                    if matched:
                        mapping[matched] = url
                        remaining_colors.remove(matched)
                        used_urls.add(url)
                        logger.debug("Vision pass 2: %s → %s (from '%s')", url, matched, description)
                    else:
                        logger.debug("Vision pass 2: %s described as '%s' — no match", url, description)
                except Exception as e:
                    logger.warning("Vision pass 2 failed for %s: %s", url, e)

        return mapping

    @staticmethod
    async def download_images(
        urls: list[str],
        max_images: int = 20,
    ) -> list[tuple[str, bytes]]:
        """Download product images for vision processing.

        Returns list of (url, image_bytes) for successfully downloaded images.
        """
        downloaded: list[tuple[str, bytes]] = []
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Lookout/1.0)"},
        ) as client:
            for url in urls[:max_images]:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "image" not in content_type and not url.lower().endswith(
                        (".jpg", ".jpeg", ".png", ".webp")
                    ):
                        continue
                    downloaded.append((url, resp.content))
                except Exception as e:
                    logger.debug("Failed to download %s: %s", url, e)
        return downloaded


class LLMClient:
    """High-level LLM client with prompt template support and structured output."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self.provider = provider or _create_default_provider()
        self.prompts_dir = prompts_dir or (Path(__file__).parent / "prompts")
        self._prompt_cache: dict[str, str] = {}

    def load_prompt(self, name: str) -> str:
        """Load a prompt template by name."""
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
        """Use LLM to structure extracted facts via structured output."""
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
            "or return an empty list."
        )

        try:
            return await self.provider.complete_structured(
                prompt,
                output_schema={
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "brand": {"type": "string"},
                        "description": {"type": "string"},
                        "features": {"type": "array", "items": {"type": "string"}},
                        "specs": {"type": "object", "additionalProperties": {"type": "string"}},
                        "materials": {"type": "string"},
                        "care": {"type": "string"},
                    },
                },
                tool_name="extract_product_facts",
                tool_description="Extract structured product facts from source data",
                system=system,
            )
        except Exception:
            logger.warning("Structured fact extraction failed, returning raw facts")
            return raw_facts

    async def extract_facts_from_markdown(
        self,
        markdown: str,
        url: str,
    ) -> dict[str, Any]:
        """Extract structured product facts from markdown content.

        Uses Claude to parse Firecrawl's clean markdown output into
        structured product data matching ExtractedFacts fields.
        """
        prompt = (
            "Extract structured product facts from the following markdown "
            "content scraped from a vendor product page.\n\n"
            "## RULES\n\n"
            "1. Only include facts explicitly stated in the text.\n"
            "2. Prefer empty fields over guessing.\n"
            "3. Copy feature bullets and specs verbatim.\n"
            "4. For images: include ONLY product photo URLs (not logos, icons, "
            "badges, or UI elements). Prefer the largest/original version — "
            "if a URL has resize params like ?w=300 or ?imwidth=246, strip them.\n"
            "5. IGNORE: navigation menus, footer links, customer service info, "
            "promotional banners ('Up to 30% Off'), pre-order FAQs, related "
            "products, and any internal system debug text.\n"
            "6. For colors: list only the color option names as shown in the "
            "product's variant selector, not colors mentioned in descriptions.\n\n"
            f"## SOURCE (markdown from {url})\n\n"
            f"{markdown}\n\n"
            "Extract the product data now."
        )

        system = (
            "You are a product data extraction assistant. Extract only product "
            "information — ignore navigation, footers, promotions, and boilerplate. "
            "NEVER invent or fabricate information. Empty fields are preferred "
            "over guesses."
        )

        try:
            return await self.provider.complete_structured(
                prompt,
                output_schema={
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "brand": {"type": "string"},
                        "description_blocks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Product description paragraphs",
                        },
                        "feature_bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Key feature bullet points",
                        },
                        "specs": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Specifications as key-value pairs",
                        },
                        "materials": {"type": "string"},
                        "care": {"type": "string"},
                        "fit_dimensions": {"type": "string"},
                        "images": {
                            "type": "array",
                            "items": {"type": "string", "format": "uri"},
                            "description": "All product image URLs found",
                        },
                        "colors": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Available color options",
                        },
                        "evidence_snippets": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "extraction_warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                tool_name="extract_product_facts",
                tool_description="Extract structured product facts from markdown content",
                system=system,
            )
        except Exception:
            logger.warning("Markdown fact extraction failed for %s", url)
            return {}

    async def generate_body_html(
        self,
        facts: dict[str, Any],
        handle: str,
        vendor: str,
    ) -> str:
        """Generate Shopify Body HTML from extracted facts.

        This uses plain text completion (not structured output) since
        the output is HTML, not JSON.
        """
        prompt_template = self.load_prompt("generate_body_html")

        prompt = prompt_template.format(
            facts=json.dumps(facts, indent=2),
            handle=handle,
            vendor=vendor,
        )

        system = (
            "You are writing product descriptions for an outdoor retail Shopify store. "
            "Use vendor facts as source material but write naturally — lead with benefits, "
            "be selective with specs, and skip measurements that don't help buying decisions. "
            "Never include review ratings, star counts, or review text in the description."
        )

        return await self.provider.complete(prompt, system)

    async def select_variant_images_vision(
        self,
        image_urls: list[str],
        color_values: list[str],
        ollama_model: str = "vision",
    ) -> dict[str, str]:
        """Select variant images using local vision model.

        Downloads product images and asks the model to pick which
        variant color name best matches each image.  The model sees
        the actual image, the list of color options, and the image URL
        (which often contains color slugs like '/basin-green/').
        """
        vision = OllamaVisionClient(model=ollama_model)

        # Deduplicate URLs (Shopify stores same image multiple times for variants)
        seen: set[str] = set()
        unique_urls: list[str] = []
        for url in image_urls:
            normalized = url.split("?")[0]
            if normalized not in seen:
                seen.add(normalized)
                unique_urls.append(url)
        if len(unique_urls) < len(image_urls):
            logger.debug("Vision: deduplicated %d → %d image URLs", len(image_urls), len(unique_urls))

        downloaded = await OllamaVisionClient.download_images(unique_urls)
        if not downloaded:
            logger.warning("Vision: no images downloaded")
            return {}

        logger.info("Vision: matching %d images to %d colors", len(downloaded), len(color_values))
        mapping = await vision.match_images_batch(downloaded, color_values)
        logger.info(
            "Vision: matched %d/%d colors: %s",
            len(mapping),
            len(color_values),
            list(mapping.keys()),
        )
        return mapping

    async def select_variant_images(
        self,
        facts: dict[str, Any],
        available_images: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Select variant images using structured output (text-based fallback)."""
        prompt_template = self.load_prompt("select_variant_images")

        prompt = prompt_template.format(
            facts=json.dumps(facts, indent=2),
            available_images=json.dumps(available_images, indent=2),
        )

        system = (
            "You are a product data assistant. Match variant options (like colors) "
            "to their corresponding product images. Only make assignments when "
            "there is clear evidence. It's better to skip uncertain matches."
        )

        # Build schema with explicit properties from known colors
        color_values = []
        for v in facts.get("variants", []):
            if v.get("option_name", "").lower() in ("color", "colour"):
                color_values = v.get("values", [])
                break

        properties = {}
        for color in color_values:
            safe_key = color.replace(" ", "_").replace("'", "").replace("/", "_")
            properties[safe_key] = {
                "type": "string",
                "description": f"Image URL for the {color} variant",
            }

        if not properties:
            return {}

        try:
            result = await self.provider.complete_structured(
                prompt,
                output_schema={
                    "type": "object",
                    "properties": properties,
                },
                tool_name="assign_variant_images",
                tool_description="Map each color variant to its best matching product image URL",
                system=system,
            )
            # Remap safe keys back to original color names
            safe_to_original = {c.replace(" ", "_").replace("'", "").replace("/", "_"): c for c in color_values}
            return {safe_to_original.get(k, k): v for k, v in result.items() if v}
        except Exception as e:
            logger.warning("Structured variant image selection failed: %s", e)
            return {}

    async def verify_description(
        self,
        facts: dict[str, Any],
        description: str,
    ) -> dict[str, Any]:
        """Verify a generated description against source facts using structured output."""
        prompt_template = self.load_prompt("verify_facts")

        prompt = prompt_template.format(
            facts=json.dumps(facts, indent=2),
            description=description,
        )

        system = (
            "You are a fact-checker for product descriptions. Verify that every "
            "claim in the description is directly supported by the source facts. "
            "Be strict — if a fact is not explicitly stated, mark it as unsupported."
        )

        try:
            result = await self.provider.complete_structured(
                prompt,
                output_schema={
                    "type": "object",
                    "properties": {
                        "supported": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Claims backed by source facts (include the supporting evidence)",
                        },
                        "unsupported": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Claims not found in the source facts",
                        },
                        "embellished": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Claims that exaggerate or stretch what the facts say",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["PASS", "FAIL"],
                            "description": "PASS if no unsupported or embellished claims, FAIL otherwise",
                        },
                    },
                    "required": ["supported", "unsupported", "embellished", "verdict"],
                },
                tool_name="fact_check_result",
                tool_description="Report fact-checking results for a product description",
                system=system,
            )
            return result
        except Exception:
            logger.warning("Structured fact-check failed")
            return {
                "supported": [],
                "unsupported": [],
                "embellished": [],
                "verdict": "ERROR",
            }


def get_llm_client(
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
    prompts_dir: Path | None = None,
) -> LLMClient:
    """Create an LLM client with default configuration.

    Prefers claude CLI provider (uses Max subscription), falls back to SDK.
    """
    provider = _create_default_provider()
    return LLMClient(provider=provider, prompts_dir=prompts_dir)
