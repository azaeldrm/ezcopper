"""
Parser for Inventory Bot Discord message format.

Expected format:
[$PRICE, DISCOUNT% off, $SAVINGS off] PRODUCT_NAME
"""

import re
from typing import Optional, List
from dataclasses import dataclass


@dataclass
class ParsedMessage:
    """Parsed Inventory Bot message."""
    price: float
    discount_percent: float
    savings: float
    product_name: str
    amazon_urls: List[str]
    raw_text: str

    def matches_rule(self, keywords: List[str], max_price: float) -> bool:
        """Check if this message matches a rule (any keyword + price limit)."""
        if self.price > max_price:
            return False

        product_lower = self.product_name.lower()
        for keyword in keywords:
            if keyword.lower().strip() in product_lower:
                return True
        return False


class MessageParser:
    """Parser for Inventory Bot message format."""

    # Pattern: [$198.00, 75.00% off, $604.62 off] Product Name (three parts)
    PRICE_PATTERN_FULL = re.compile(
        r'\[\$?([\d,]+\.?\d*)\s*,\s*'  # Price: $198.00
        r'([\d.]+)%?\s*off\s*,\s*'     # Discount: 75.00% off
        r'\$?([\d,]+\.?\d*)\s*off\]'   # Savings: $604.62 off
        r'\s*(.+?)(?:\n|$)',           # Product name (until newline)
        re.IGNORECASE
    )

    # Pattern: [$1,114.75, 54.40% off] Product Name (two parts, no savings)
    PRICE_PATTERN_TWO = re.compile(
        r'\[\$?([\d,]+\.?\d*)\s*,\s*'  # Price: $1,114.75
        r'([\d.]+)%?\s*off\]'          # Discount: 54.40% off
        r'\s*(.+?)(?:\n|$)',           # Product name (until newline)
        re.IGNORECASE
    )

    # Fallback: just price and product name
    SIMPLE_PRICE_PATTERN = re.compile(
        r'\[\$?([\d,]+\.?\d*)[^\]]*\]\s*(.+?)(?:\n|$)',
        re.IGNORECASE
    )

    # Amazon URL patterns
    AMAZON_URL_PATTERNS = [
        re.compile(r'https?://(?:www\.)?amazon\.com[^\s<>"]*', re.IGNORECASE),
        re.compile(r'https?://(?:www\.)?amzn\.to[^\s<>"]*', re.IGNORECASE),
        re.compile(r'https?://(?:www\.)?amazon\.co\.[a-z]{2}[^\s<>"]*', re.IGNORECASE),
        re.compile(r'https?://a\.co[^\s<>"]*', re.IGNORECASE),
    ]

    @classmethod
    def parse(cls, text: str, urls: List[str] = None) -> Optional[ParsedMessage]:
        """
        Parse an Inventory Bot message.

        Args:
            text: The message text
            urls: Optional list of URLs already extracted from the message

        Returns:
            ParsedMessage if successfully parsed, None otherwise
        """
        urls = urls or []

        # Try full pattern first: [$198.00, 75.00% off, $604.62 off] Product
        match = cls.PRICE_PATTERN_FULL.search(text)
        if match:
            try:
                price = float(match.group(1).replace(',', ''))
                discount = float(match.group(2))
                savings = float(match.group(3).replace(',', ''))
                product_name = match.group(4).strip()

                amazon_urls = cls._extract_amazon_urls(text, urls)

                return ParsedMessage(
                    price=price,
                    discount_percent=discount,
                    savings=savings,
                    product_name=product_name,
                    amazon_urls=amazon_urls,
                    raw_text=text
                )
            except (ValueError, IndexError):
                pass

        # Try two-part pattern: [$1,114.75, 54.40% off] Product
        match = cls.PRICE_PATTERN_TWO.search(text)
        if match:
            try:
                price = float(match.group(1).replace(',', ''))
                discount = float(match.group(2))
                product_name = match.group(3).strip()

                amazon_urls = cls._extract_amazon_urls(text, urls)

                return ParsedMessage(
                    price=price,
                    discount_percent=discount,
                    savings=0.0,
                    product_name=product_name,
                    amazon_urls=amazon_urls,
                    raw_text=text
                )
            except (ValueError, IndexError):
                pass

        # Fallback: just price and product name
        match = cls.SIMPLE_PRICE_PATTERN.search(text)
        if match:
            try:
                price = float(match.group(1).replace(',', ''))
                product_name = match.group(2).strip()

                amazon_urls = cls._extract_amazon_urls(text, urls)

                return ParsedMessage(
                    price=price,
                    discount_percent=0.0,
                    savings=0.0,
                    product_name=product_name,
                    amazon_urls=amazon_urls,
                    raw_text=text
                )
            except (ValueError, IndexError):
                pass

        return None

    @classmethod
    def _extract_amazon_urls(cls, text: str, existing_urls: List[str]) -> List[str]:
        """Extract Amazon URLs from text and existing URL list."""
        amazon_urls = []

        # Check existing URLs
        for url in existing_urls:
            for pattern in cls.AMAZON_URL_PATTERNS:
                if pattern.match(url):
                    amazon_urls.append(url)
                    break

        # Search in text
        for pattern in cls.AMAZON_URL_PATTERNS:
            matches = pattern.findall(text)
            amazon_urls.extend(matches)

        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in amazon_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        return unique_urls


def test_parser():
    """Test the parser with sample messages."""
    sample = """[$198.00, 75.00% off, $604.62 off] Homestyles 6660-328C 5 Piece Outdoor Dining Set, 48" Table, Charcoal
@po10% @po20% @po30% @po40% @po50% @po60% @po70%
Amazon
https://www.amazon.com/dp/B0XXXXXXXXX"""

    result = MessageParser.parse(sample)
    if result:
        print(f"Price: ${result.price}")
        print(f"Discount: {result.discount_percent}%")
        print(f"Savings: ${result.savings}")
        print(f"Product: {result.product_name}")
        print(f"URLs: {result.amazon_urls}")
        print(f"Matches 'dining, outdoor' @ $250: {result.matches_rule(['dining', 'outdoor'], 250)}")
        print(f"Matches 'dining, outdoor' @ $100: {result.matches_rule(['dining', 'outdoor'], 100)}")
    else:
        print("Failed to parse")


if __name__ == "__main__":
    test_parser()
