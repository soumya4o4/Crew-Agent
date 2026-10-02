"""Forex pricing: pure functions, no I/O. We buy at the live mid-market rate, add a margin, and charge a small flat fee
(free on bigger orders). The numbers below are the prototype's price list; change them here only."""

MARGIN = {"cash": 0.018, "card": 0.010}        # added to the mid-market rate
FEE_INR = {"cash": 150, "card": 99}            # delivery (cash) or card issue/load (card), GST included
FREE_FEE_ABOVE_INR = 50_000
MIN_ORDER_INR = 2_000
MAX_ORDER_INR = 2_000_000
CASH_LIMIT_USD = 3_000                          # RBI's limit on foreign cash taken out per trip
PRODUCT_LABEL = {"cash": "Cash", "card": "Forex card"}
PRODUCT_ICON = {"cash": "💵", "card": "💳"}


def quote(mid: float, amount: float, product: str) -> dict:
    """What `amount` units of a currency cost in rupees. mid = rupees per 1 unit at the market rate."""
    rate = mid * (1 + MARGIN[product])
    subtotal = round(amount * rate)
    fee = 0 if subtotal >= FREE_FEE_ABOVE_INR else FEE_INR[product]
    return {"amount": amount, "product": product, "mid": mid, "rate": rate, "subtotal": subtotal, "fee": fee,
            "total": subtotal + fee, "margin_pct": MARGIN[product] * 100}


def limit_problem(inr_value: float, usd_mid: float | None, product: str | None = None) -> str | None:
    """Why this order can't go ahead (None if it can). inr_value is the market value of the order in rupees."""
    if inr_value < MIN_ORDER_INR:
        return f"The smallest order is ₹{MIN_ORDER_INR:,}. Please enter a bigger amount."
    if inr_value > MAX_ORDER_INR:
        return f"For orders above ₹{MAX_ORDER_INR // 100_000} lakh our forex desk helps directly. Please enter a smaller amount."
    if product == "cash" and usd_mid and inr_value / usd_mid > CASH_LIMIT_USD:
        return f"Cash is limited to about ${CASH_LIMIT_USD:,} per trip by RBI rules. A forex card has no such cap."
    return None


def round_amount(value: float) -> float:
    """A friendly amount to buy: whole units for big numbers, 2 decimals for tiny ones (like when 1 unit is worth many rupees)."""
    if value >= 1000:
        return float(round(value / 50) * 50)
    if value >= 100:
        return float(round(value / 10) * 10)
    return float(round(value))


def fmt_amount(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
