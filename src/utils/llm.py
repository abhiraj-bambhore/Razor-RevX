"""
Gemini LLM client wrapper.
Uses the new google-genai SDK (google.genai).
Falls back to deterministic logic when no API key is available or key is invalid.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _get_gemini_client():
    """Lazy init the Gemini client. Returns None if no key or init fails."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    # Try new google-genai SDK first
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        logger.debug("Gemini client initialized via google-genai SDK")
        return ("genai", client)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("google-genai init failed: %s", exc)

    # Fallback: legacy google-generativeai SDK
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        model = genai_legacy.GenerativeModel("gemini-1.5-flash")
        logger.debug("Gemini client initialized via legacy google-generativeai SDK")
        return ("legacy", model)
    except Exception as exc:
        logger.warning("Failed to init Gemini (both SDKs): %s. Using deterministic fallback.", exc)
        return None


# Singleton
_client: Optional[tuple] = None
_client_initialized = False


def get_client():
    global _client, _client_initialized
    if not _client_initialized:
        _client = _get_gemini_client()
        _client_initialized = True
    return _client


def _call_gemini(prompt: str) -> str:
    """
    Call Gemini with prompt, handling both SDK versions.
    Raises on failure so callers can catch and fallback.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("No Gemini client available")

    sdk_type, sdk_obj = client

    if sdk_type == "genai":
        response = sdk_obj.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip()
    else:
        # Legacy SDK
        response = sdk_obj.generate_content(prompt)
        return response.text.strip()



def llm_risk_assessment(event_summary: str) -> dict:
    """
    Ask Gemini to assess risk and recommend intervention.

    Returns:
        {
            "risk_score": float,       # 0-100
            "reasoning": str,          # Why this score
            "recommended_action": str, # What to do
            "estimated_recovery_probability": float,  # 0-1
            "llm_used": bool,
        }
    """
    if get_client() is None:
        return _deterministic_fallback(event_summary)

    prompt = f"""You are a payment recovery specialist AI for Razorpay. Analyze this revenue-at-risk event and respond with a JSON object.

EVENT:
{event_summary}

Respond ONLY with valid JSON (no markdown, no explanation outside JSON):
{{
    "risk_score": <0-100, higher means more urgent>,
    "reasoning": "<2-3 sentence analysis of why this score>",
    "recommended_action": "<one of: auto_retry, send_recovery_link, send_nudge, mandate_retry, email_reminder, phone_followup, plan_downgrade_offer, escalate_to_human, do_not_retry>",
    "estimated_recovery_probability": <0.0-1.0>
}}

Rules:
- fraud_flagged events MUST get risk_score 95+ and recommended_action "escalate_to_human"
- gateway_error/network_error/timeout = transient, recommend auto_retry
- insufficient_funds = send_recovery_link after delay
- High-value cases (>INR 50,000) should have higher risk scores
- Premium/Enterprise customers should have higher urgency
"""

    try:
        text = _call_gemini(prompt)

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[:text.rfind("```")]
            text = text.strip()

        result = json.loads(text)
        result["llm_used"] = True
        return result
    except Exception as exc:
        logger.warning("Gemini call failed: %s. Using fallback.", exc)
        return _deterministic_fallback(event_summary)


def llm_compose_recovery_message(
    event_type: str,
    customer_name: str,
    amount: float,
    failure_reason: str,
    channel: str = "sms",
) -> str:
    """
    Compose a personalized recovery message using Gemini.
    Falls back to templates if LLM unavailable.
    """
    if get_client() is None:
        return _template_message(event_type, customer_name, amount, failure_reason, channel)

    prompt = f"""Compose a brief, professional {channel.upper()} message for recovering a payment.

Customer: {customer_name}
Amount: INR {amount:,.2f}
Failure reason: {failure_reason}
Event type: {event_type}

Rules:
- Be empathetic, not aggressive
- Include the amount
- If SMS, keep under 160 characters
- Do NOT use the words "failed" or "failure" -- say "couldn't be processed" or "needs attention"
- End with a clear call to action
- Use Hinglish if failure_reason is insufficient_funds or invalid_otp (mix Hindi + English naturally)

Respond with ONLY the message text, nothing else.
"""

    try:
        return _call_gemini(prompt)
    except Exception:
        return _template_message(event_type, customer_name, amount, failure_reason, channel)


def _deterministic_fallback(event_summary: str) -> dict:
    """Rule-based fallback when LLM is unavailable."""
    summary_lower = event_summary.lower()

    # Parse amount if present
    risk_score = 50.0
    recovery_prob = 0.5
    action = "send_recovery_link"

    if "fraud" in summary_lower:
        risk_score = 95.0
        recovery_prob = 0.05
        action = "escalate_to_human"
    elif "gateway_error" in summary_lower or "network_error" in summary_lower or "timeout" in summary_lower:
        risk_score = 70.0
        recovery_prob = 0.85
        action = "auto_retry"
    elif "insufficient_funds" in summary_lower:
        risk_score = 60.0
        recovery_prob = 0.55
        action = "send_recovery_link"
    elif "invalid_otp" in summary_lower:
        risk_score = 55.0
        recovery_prob = 0.60
        action = "send_recovery_link"
    elif "card_declined" in summary_lower:
        risk_score = 65.0
        recovery_prob = 0.30
        action = "send_recovery_link"
    elif "checkout" in summary_lower and "abandon" in summary_lower:
        risk_score = 55.0
        recovery_prob = 0.45
        action = "send_nudge"
    elif "subscription" in summary_lower:
        risk_score = 60.0
        recovery_prob = 0.60
        action = "mandate_retry"
    elif "overdue" in summary_lower or "receivable" in summary_lower:
        risk_score = 65.0
        recovery_prob = 0.50
        action = "email_reminder"

    # Boost for high amounts
    if "₹" in event_summary:
        # Try to extract amount
        try:
            import re
            match = re.search(r"₹([\d,]+\.?\d*)", event_summary)
            if match:
                amt = float(match.group(1).replace(",", ""))
                if amt >= 50000:
                    risk_score = min(100, risk_score + 20)
                elif amt >= 10000:
                    risk_score = min(100, risk_score + 10)
        except (ValueError, AttributeError):
            pass

    # Boost for premium/enterprise
    if "premium" in summary_lower or "enterprise" in summary_lower:
        risk_score = min(100, risk_score + 10)
        recovery_prob = min(1.0, recovery_prob + 0.1)

    return {
        "risk_score": round(risk_score, 1),
        "reasoning": f"Deterministic assessment based on failure pattern. Key factors detected in event.",
        "recommended_action": action,
        "estimated_recovery_probability": round(recovery_prob, 3),
        "llm_used": False,
    }


def _template_message(
    event_type: str,
    customer_name: str,
    amount: float,
    failure_reason: str,
    channel: str,
) -> str:
    """Template-based message when LLM is unavailable."""
    name = customer_name.split()[0] if customer_name else "there"

    templates = {
        "payment_failure": {
            "sms": f"Hi {name}, your payment of INR {amount:,.0f} needs attention. Retry here: {{link}} - Payments Team",
            "email": f"Hi {name},\n\nYour recent payment of INR {amount:,.2f} couldn't be processed. Please retry using the link below.\n\nRetry: {{link}}\n\nRegards,\nPayments Team",
            "whatsapp": f"Hi {name}, your payment of INR {amount:,.0f} could not be processed. Please complete payment using this link: {{link}}",
        },
        "checkout_abandonment": {
            "sms": f"Hi {name}, you left items in your cart (INR {amount:,.0f}). Complete checkout: {{link}}",
            "email": f"Hi {name},\n\nLooks like you left some items in your cart worth INR {amount:,.2f}. Your cart is saved -- complete your purchase here.\n\nCheckout: {{link}}",
            "whatsapp": f"Hi {name}, your cart is saved with items worth INR {amount:,.0f}. Complete purchase here: {{link}}",
        },
        "subscription_failure": {
            "sms": f"Hi {name}, your subscription renewal of INR {amount:,.0f} needs attention. Update: {{link}}",
            "email": f"Hi {name},\n\nYour subscription renewal of INR {amount:,.2f} couldn't be processed. To avoid service interruption, please update your payment method.\n\nUpdate: {{link}}",
            "whatsapp": f"Hi {name}, your subscription renewal of INR {amount:,.0f} needs attention. Update payment method here: {{link}}",
        },
        "receivable_overdue": {
            "sms": f"Reminder: Invoice of INR {amount:,.0f} is pending. Pay now: {{link}} - Accounts Team",
            "email": f"Dear {name},\n\nThis is a friendly reminder that invoice amount INR {amount:,.2f} is overdue. Please arrange payment at your earliest convenience.\n\nPay: {{link}}\n\nRegards,\nAccounts Receivable",
            "whatsapp": f"Hi {name}, invoice of INR {amount:,.0f} is pending. Please complete payment using this link: {{link}}",
        },
    }

    mode_key = event_type.replace(".", "_").replace("charged_halted", "failure")
    # Normalize to our template keys
    for key in templates:
        if key in mode_key or mode_key in key:
            mode_key = key
            break

    channel_key = channel.lower()
    if channel_key not in ("sms", "email", "whatsapp"):
        channel_key = "sms"

    return templates.get(mode_key, templates["payment_failure"]).get(channel_key, f"Payment of ₹{amount:,.0f} needs attention. Link: {{link}}")
