"""
Shopify API placeholder module.

This module provides stubbed interfaces for future Shopify Admin API integration.
It defines the interface that will be implemented when API access is added.

NOT IMPLEMENTED: Actual API calls are not performed. This is for design purposes only.
"""

from abc import ABC, abstractmethod
from typing import Any

from .models import MerchOutput, OutputImage


class ShopifyAPIError(Exception):
    """Base exception for Shopify API errors."""

    pass


class ShopifyAPINotImplementedError(ShopifyAPIError):
    """Raised when API methods are called but not yet implemented."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"Shopify API method '{method}' is not yet implemented. "
            "This is a placeholder for future development."
        )


class ShopifyAPIClient(ABC):
    """
    Abstract base class for Shopify API clients.

    This defines the interface that will be used for Shopify API integration.
    Implementations will use the Shopify Admin API to push product updates.
    """

    @abstractmethod
    async def upsert_product(
        self,
        handle: str,
        merch_output: MerchOutput,
    ) -> dict[str, Any]:
        """
        Update or create a product with merchandising data.

        This method should:
        1. Find the product by handle
        2. Update body HTML if provided
        3. Return the updated product data

        Args:
            handle: The product handle (unique identifier).
            merch_output: The merchandising output with content to update.

        Returns:
            Dictionary with the updated product data from Shopify.

        Raises:
            ShopifyAPIError: If the API call fails.
        """
        pass

    @abstractmethod
    async def attach_images(
        self,
        handle: str,
        images: list[OutputImage],
    ) -> list[dict[str, Any]]:
        """
        Attach images to a product.

        This method should:
        1. Find the product by handle
        2. Upload/attach images in order
        3. Set alt text for each image
        4. Return the image data

        Args:
            handle: The product handle.
            images: List of images to attach.

        Returns:
            List of dictionaries with image data from Shopify.

        Raises:
            ShopifyAPIError: If the API call fails.
        """
        pass

    @abstractmethod
    async def assign_variant_images(
        self,
        handle: str,
        variant_image_map: dict[str, str | list[str]],
    ) -> dict[str, Any]:
        """
        Assign images to product variants.

        This method should:
        1. Find the product by handle
        2. Match variants by option value (typically color)
        3. Assign the specified image to each variant
        4. Return the updated variant data

        Args:
            handle: The product handle.
            variant_image_map: Mapping of option value (e.g., color name) to image URL.

        Returns:
            Dictionary with the updated variant data from Shopify.

        Raises:
            ShopifyAPIError: If the API call fails.
        """
        pass

    @abstractmethod
    async def get_product_by_handle(
        self,
        handle: str,
    ) -> dict[str, Any] | None:
        """
        Retrieve a product by its handle.

        Args:
            handle: The product handle.

        Returns:
            Product data dictionary, or None if not found.
        """
        pass


class PlaceholderShopifyClient(ShopifyAPIClient):
    """
    Placeholder implementation of the Shopify API client.

    This class provides stub implementations that raise NotImplementedError.
    It serves as documentation and a template for future implementation.
    """

    def __init__(
        self,
        shop_url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """
        Initialize the placeholder client.

        In the future implementation, these credentials will be used
        to authenticate with the Shopify Admin API.

        Args:
            shop_url: The Shopify store URL (e.g., "mystore.myshopify.com").
            api_key: Shopify API key (for private apps).
            api_secret: Shopify API secret (for private apps).
            access_token: Shopify access token (for custom apps/OAuth).
        """
        self.shop_url = shop_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token

    async def upsert_product(
        self,
        handle: str,
        merch_output: MerchOutput,
    ) -> dict[str, Any]:
        """
        Placeholder for product upsert.

        Future implementation notes:
        - Use GraphQL Admin API for efficiency
        - Mutation: productUpdate or productCreate
        - Handle rate limiting (max 2 requests/second)
        - Support idempotent updates

        Example GraphQL mutation:
        ```graphql
        mutation productUpdate($input: ProductInput!) {
            productUpdate(input: $input) {
                product {
                    id
                    handle
                    descriptionHtml
                }
                userErrors {
                    field
                    message
                }
            }
        }
        ```
        """
        raise ShopifyAPINotImplementedError("upsert_product")

    async def attach_images(
        self,
        handle: str,
        images: list[OutputImage],
    ) -> list[dict[str, Any]]:
        """
        Placeholder for image attachment.

        Future implementation notes:
        - Use productCreateMedia mutation for images
        - Images should be uploaded as staged uploads first
        - Set position and alt text
        - Handle rate limiting

        Example flow:
        1. Create staged upload targets
        2. Upload image files to staged URLs
        3. Create media from staged uploads
        4. Assign media to product
        """
        raise ShopifyAPINotImplementedError("attach_images")

    async def assign_variant_images(
        self,
        handle: str,
        variant_image_map: dict[str, str | list[str]],
    ) -> dict[str, Any]:
        """
        Placeholder for variant image assignment.

        Future implementation notes:
        - First attach images to product if not already present
        - Use productVariantUpdate mutation
        - Match variants by option value (Color, etc.)
        - Handle rate limiting

        Example GraphQL mutation:
        ```graphql
        mutation productVariantUpdate($input: ProductVariantInput!) {
            productVariantUpdate(input: $input) {
                productVariant {
                    id
                    image {
                        id
                    }
                }
                userErrors {
                    field
                    message
                }
            }
        }
        ```
        """
        raise ShopifyAPINotImplementedError("assign_variant_images")

    async def get_product_by_handle(
        self,
        handle: str,
    ) -> dict[str, Any] | None:
        """
        Placeholder for product retrieval.

        Future implementation notes:
        - Use GraphQL query with handle filter
        - Return None if product not found
        - Include variants and images in response

        Example GraphQL query:
        ```graphql
        query getProductByHandle($handle: String!) {
            productByHandle(handle: $handle) {
                id
                handle
                title
                descriptionHtml
                images(first: 20) {
                    edges {
                        node {
                            id
                            src
                            altText
                        }
                    }
                }
                variants(first: 100) {
                    edges {
                        node {
                            id
                            title
                            image {
                                id
                            }
                            selectedOptions {
                                name
                                value
                            }
                        }
                    }
                }
            }
        }
        ```
        """
        raise ShopifyAPINotImplementedError("get_product_by_handle")


# Type alias for future use
ShopifyClient = ShopifyAPIClient


def get_shopify_client(
    shop_url: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    access_token: str | None = None,
) -> ShopifyClient:
    """
    Factory function to create a Shopify API client.

    Currently returns a placeholder client. In the future, this will
    return a fully implemented client based on the provided credentials.

    Args:
        shop_url: The Shopify store URL.
        api_key: Shopify API key.
        api_secret: Shopify API secret.
        access_token: Shopify access token.

    Returns:
        ShopifyClient instance (currently placeholder).
    """
    return PlaceholderShopifyClient(
        shop_url=shop_url,
        api_key=api_key,
        api_secret=api_secret,
        access_token=access_token,
    )


# Example of how the API will be used in the future:
"""
async def push_to_shopify(
    merch_output: MerchOutput,
    client: ShopifyClient,
) -> None:
    '''
    Push merchandising output to Shopify.

    This is an example of how the future API integration will work.
    '''
    handle = merch_output.handle

    # Update product description
    if merch_output.body_html:
        await client.upsert_product(handle, merch_output)

    # Attach images
    if merch_output.images:
        await client.attach_images(handle, merch_output.images)

    # Assign variant images
    if merch_output.variant_image_map:
        await client.assign_variant_images(handle, merch_output.variant_image_map)
"""
