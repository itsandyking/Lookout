"""
Web scraper for vendor product pages.

Supports both static (requests/httpx) and dynamic (Playwright) scraping
based on vendor configuration.
"""

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import PlaywrightConfig, VendorConfig

logger = logging.getLogger(__name__)


class ScrapedPage:
    """Represents a scraped web page."""

    def __init__(
        self,
        url: str,
        html: str,
        status_code: int = 200,
        final_url: str | None = None,
        error: str | None = None,
    ) -> None:
        self.url = url
        self.html = html
        self.status_code = status_code
        self.final_url = final_url or url
        self.error = error

    @property
    def success(self) -> bool:
        """Check if the scrape was successful."""
        return self.error is None and self.status_code == 200 and bool(self.html)


class WebScraper:
    """
    Web scraper that supports both static and dynamic content.

    Uses httpx for static pages and Playwright for JavaScript-heavy sites.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        min_delay_ms: int = 500,
        max_delay_ms: int = 2000,
    ) -> None:
        """
        Initialize the scraper.

        Args:
            http_client: Optional shared HTTP client for static scraping.
            min_delay_ms: Minimum delay between requests.
            max_delay_ms: Maximum delay between requests.
        """
        self._client = http_client
        self._owns_client = http_client is None
        self._min_delay = min_delay_ms / 1000
        self._max_delay = max_delay_ms / 1000
        self._browser = None
        self._playwright = None
        self._browser_lock = asyncio.Lock()

    async def __aenter__(self) -> "WebScraper":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()

        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def scrape(
        self,
        url: str,
        vendor_config: VendorConfig,
    ) -> ScrapedPage:
        """
        Scrape a URL using the appropriate method.

        Args:
            url: The URL to scrape.
            vendor_config: Vendor configuration for scraping behavior.

        Returns:
            ScrapedPage with the HTML content.
        """
        # Add polite delay
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

        if vendor_config.use_playwright:
            return await self._scrape_dynamic(url, vendor_config.playwright_config)
        else:
            return await self._scrape_static(url)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    )
    async def _scrape_static(self, url: str) -> ScrapedPage:
        """
        Scrape a static page using httpx.

        Args:
            url: The URL to scrape.

        Returns:
            ScrapedPage with the HTML content.
        """
        try:
            response = await self._client.get(url)
            response.raise_for_status()

            return ScrapedPage(
                url=url,
                html=response.text,
                status_code=response.status_code,
                final_url=str(response.url),
            )

        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error scraping {url}: {e.response.status_code}")
            return ScrapedPage(
                url=url,
                html="",
                status_code=e.response.status_code,
                error=f"HTTP {e.response.status_code}",
            )

        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return ScrapedPage(
                url=url,
                html="",
                status_code=0,
                error=str(e),
            )

    @staticmethod
    def _is_bot_blocked(html: str) -> bool:
        """Check if the page is a bot protection / waiting room."""
        markers = [
            "sit tight",
            "hands full at the moment",
            "checking your browser",
            "access denied",
            "please verify you are a human",
            "ray id",
            "just a moment",
            "enable javascript",
        ]
        html_lower = html.lower()
        return any(marker in html_lower for marker in markers)

    @staticmethod
    async def _has_meaningful_content(page: Any) -> bool:
        """Check if page has meaningful product content even without selector match."""
        return await page.evaluate(
            "Boolean(document.querySelector('h1') "
            "|| document.querySelector('script[type=\"application/ld+json\"]') "
            "|| document.querySelector('[itemtype*=\"Product\"]'))"
        )

    async def _scrape_dynamic(
        self,
        url: str,
        config: PlaywrightConfig,
    ) -> ScrapedPage:
        """
        Scrape a dynamic page using Playwright.

        Args:
            url: The URL to scrape.
            config: Playwright configuration.

        Returns:
            ScrapedPage with the rendered HTML content.
        """
        try:
            # Lazy-load Playwright with lock to prevent race condition
            if self._playwright is None:
                async with self._browser_lock:
                    if self._playwright is None:
                        from playwright.async_api import async_playwright

                        self._playwright = await async_playwright().start()
                        self._browser = await self._playwright.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--disable-dev-shm-usage",
                                "--no-sandbox",
                                "--disable-setuid-sandbox",
                                "--disable-infobars",
                                "--disable-extensions",
                            ],
                        )

            # Create a new context and page
            context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            try:
                page = await context.new_page()

                # Add stealth script to avoid bot detection
                await page.add_init_script("""
                    // Override webdriver flag
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    // Override plugins (headless has 0 plugins)
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    // Override languages
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    // Override platform
                    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
                """)

                # Navigate to the page
                response = await page.goto(
                    url,
                    timeout=config.wait_timeout_ms,
                    wait_until="domcontentloaded",
                )

                # Wait for specific selector with progressive timeout
                if config.wait_for_selector:
                    first_timeout = 3000
                    try:
                        await page.wait_for_selector(
                            config.wait_for_selector,
                            timeout=first_timeout,
                        )
                    except Exception:
                        # Selector didn't match in first attempt — check for content
                        has_content = await self._has_meaningful_content(page)
                        if has_content:
                            logger.info(
                                "Selector didn't match on %s but meaningful content found, proceeding",
                                url,
                            )
                        else:
                            # Page looks empty, retry with remaining budget
                            remaining = max(config.wait_timeout_ms - first_timeout, 1000)
                            try:
                                await page.wait_for_selector(
                                    config.wait_for_selector,
                                    timeout=remaining,
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Selector wait failed for {url}: {e}. "
                                    "Continuing with current content."
                                )

                # Additional wait for JS rendering
                if config.extra_wait_ms > 0:
                    await asyncio.sleep(config.extra_wait_ms / 1000)

                # Get the rendered HTML
                html = await page.content()
                final_url = page.url
                status_code = response.status if response else 200

                # Check for bot protection / waiting room
                if self._is_bot_blocked(html):
                    logger.warning(
                        "Bot protection detected on %s, retrying with fresh context",
                        url,
                    )
                    await context.close()

                    # Retry with different viewport and user agent
                    context = await self._browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"
                        ),
                        locale="en-US",
                    )
                    page = await context.new_page()
                    await page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => false });
                        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    """)

                    response = await page.goto(
                        url,
                        timeout=config.wait_timeout_ms,
                        wait_until="networkidle",
                    )

                    if config.wait_for_selector:
                        try:
                            await page.wait_for_selector(
                                config.wait_for_selector,
                                timeout=config.wait_timeout_ms,
                            )
                        except Exception:
                            pass

                    if config.extra_wait_ms > 0:
                        await asyncio.sleep(config.extra_wait_ms / 1000)

                    html = await page.content()
                    final_url = page.url
                    status_code = response.status if response else 200

                    if self._is_bot_blocked(html):
                        logger.warning("Bot protection persists on %s after retry", url)

                return ScrapedPage(
                    url=url,
                    html=html,
                    status_code=status_code,
                    final_url=final_url,
                )

            finally:
                await context.close()

        except Exception as e:
            logger.error(f"Playwright error scraping {url}: {e}")
            return ScrapedPage(
                url=url,
                html="",
                status_code=0,
                error=str(e),
            )

    async def save_html(
        self,
        page: ScrapedPage,
        artifacts_dir: Path,
    ) -> Path:
        """
        Save the scraped HTML to the artifacts directory.

        Args:
            page: The scraped page.
            artifacts_dir: Path to the artifacts directory.

        Returns:
            Path to the saved HTML file.
        """
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        html_path = artifacts_dir / "source.html"

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.html)

        return html_path


async def scrape_url(
    url: str,
    vendor_config: VendorConfig,
    http_client: httpx.AsyncClient | None = None,
) -> ScrapedPage:
    """
    Convenience function to scrape a single URL.

    Args:
        url: The URL to scrape.
        vendor_config: Vendor configuration.
        http_client: Optional shared HTTP client.

    Returns:
        ScrapedPage with the HTML content.
    """
    async with WebScraper(http_client=http_client) as scraper:
        return await scraper.scrape(url, vendor_config)
