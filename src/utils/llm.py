"""
Gemini LLM client wrapper with 3-tier resilience.

Tier 1: Gemini 2.0 Flash (contextual, nuanced AI scoring)
Tier 2: ML model (GradientBoosting + RandomForest)
Tier 3: Static heuristic rules (absolute last resort)

Falls back gracefully when API key is missing, rate-limited, or network fails.
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
        logger.warning("Failed to init Gemini (both SDKs): %s. Using ML/deterministic fallback.", exc)
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
    3-tier risk assessment:
        Tier 1: Ask Gemini LLM
        Tier 2: ML model (GradientBoosting)
        Tier 3: Deterministic heuristic (last resort)

    Returns:
        {
            "risk_score": float,       # 0-100
            "reasoning": str,          # Why this score
            "recommended_action": str, # What to do
            "estimated_recovery_probability": float,  # 0-1
            "llm_used": bool,
            "model_used": str,         # "gemini_llm" | "ml_gradient_boosting" | "heuristic"
        }
    """
    # ── Tier 1: Gemini LLM ───────────────────────────────────────────────
    if get_client() is not None:
        try:
            return _gemini_risk_assessment(event_summary)
        except Exception as exc:
            logger.warning("Tier 1 (Gemini) failed: %s. Falling back to ML model.", exc)

    # ── Tier 2: ML Model ─────────────────────────────────────────────────
    ml_result = _ml_risk_assessment(event_summary)
    if ml_result is not None:
        return ml_result

    # ── Tier 3: Heuristic (last resort) ──────────────────────────────────
    logger.warning("Tier 2 (ML) unavailable. Using Tier 3 heuristic fallback.")
    return _deterministic_fallback(event_summary)


def _gemini_risk_assessment(event_summary: str) -> dict:
    """Tier 1: Gemini LLM risk assessment."""
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

    text = _call_gemini(prompt)

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if "```" in text:
            text = text[:text.rfind("```")]
        text = text.strip()

    result = json.loads(text)
    result["llm_used"] = True
    result["model_used"] = "gemini_llm"
    return result


def _ml_risk_assessment(event_summary: str) -> dict | None:
    """
    Tier 2: ML model risk assessment using GradientBoosting.

    Note: This requires the event object for feature extraction.
    When called from llm_risk_assessment (text summary only),
    we parse what we can from the summary text.
    Falls back to None if ML model unavailable.
    """
    try:
        from src.ml.risk_model import get_risk_model
        model = get_risk_model()
        if not model.is_fitted:
            return None

        # Parse basic features from the text summary for standalone calls
        result = _ml_predict_from_summary(model, event_summary)
        return result
    except Exception as exc:
        logger.warning("Tier 2 (ML) failed: %s", exc)
        return None


def _ml_predict_from_summary(model, event_summary: str) -> dict:
    """
    Extract features from text summary and predict using ML model.
    Used when only the text summary is available (not the event object).
    """
    import numpy as np
    summary_lower = event_summary.lower()

    # Build a pseudo feature vector from text parsing
    features = [0.0] * 18

    # Parse amount
    import re
    amount_match = re.search(r"₹([\d,]+\.?\d*)", event_summary)
    if amount_match:
        amt = float(amount_match.group(1).replace(",", ""))
        features[0] = np.log1p(amt)
        features[9] = 1.0 if amt >= 50000 else 0.0

    # Segment
    if "enterprise" in summary_lower:
        features[1] = 4.0
        features[10] = 1.0
    elif "premium" in summary_lower:
        features[1] = 3.0
        features[10] = 1.0
    elif "vip" in summary_lower:
        features[1] = 2.0
    elif "returning" in summary_lower:
        features[1] = 1.0

    # Error reason
    reason_map = {
        "insufficient_funds": (0, False, False),
        "invalid_otp": (1, False, False),
        "gateway_error": (2, True, False),
        "card_declined": (3, False, False),
        "payment_cancelled": (4, False, False),
        "network_error": (5, True, False),
        "timeout": (6, True, False),
        "fraud_flagged": (7, False, True),
    }
    for reason_text, (idx, transient, fraud) in reason_map.items():
        if reason_text in summary_lower:
            features[4] = float(idx)
            features[7] = 1.0 if transient else 0.0
            features[8] = 1.0 if fraud else 0.0
            break

    # Event type
    if "checkout" in summary_lower:
        features[16] = 1.0
    elif "subscription" in summary_lower:
        features[16] = 2.0
    elif "receivable" in summary_lower or "overdue" in summary_lower:
        features[16] = 3.0

    # Days overdue
    days_match = re.search(r"(\d+)\s*days?\s*overdue", summary_lower)
    if days_match:
        features[11] = float(days_match.group(1))

    # Use model's internal prediction
    X = np.array([features], dtype=np.float64)
    X_scaled = model.scaler.transform(X)

    risk_score = float(np.clip(model.risk_regressor.predict(X_scaled)[0], 0, 100))
    action_idx = int(model.action_classifier.predict(X_scaled)[0])

    from src.ml.risk_model import IDX_TO_ACTION
    action = IDX_TO_ACTION.get(action_idx, "send_recovery_link")

    # Recovery probability
    if risk_score >= 85:
        recovery_prob = 0.15
    elif risk_score >= 65:
        recovery_prob = 0.45
    elif risk_score >= 40:
        recovery_prob = 0.65
    else:
        recovery_prob = 0.80

    return {
        "risk_score": round(risk_score, 1),
        "reasoning": (
            f"Machine learning model evaluation (Gradient Boosting Regressor). "
            f"Evaluated risk score at {risk_score:.1f} and recommended {action}."
        ),
        "recommended_action": action,
        "estimated_recovery_probability": round(recovery_prob, 3),
        "llm_used": False,
        "model_used": "ml_gradient_boosting",
    }


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
    Supports Hinglish for specific failure reasons and voice channel.
    """
    if get_client() is None:
        return _template_message(event_type, customer_name, amount, failure_reason, channel)

    # Determine if Hinglish should be used
    use_hinglish = failure_reason in ("insufficient_funds", "invalid_otp") or channel == "voice"

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
{"- Use Hinglish (mix Hindi + English naturally, e.g., 'Aapka payment process nahi ho paya')" if use_hinglish else ""}
{"- This is for a VOICE IVR call — make it conversational and warm, include pauses" if channel == "voice" else ""}

Respond with ONLY the message text, nothing else.
"""

    try:
        return _call_gemini(prompt)
    except Exception:
        return _template_message(event_type, customer_name, amount, failure_reason, channel)


def _deterministic_fallback(event_summary: str) -> dict:
    """Tier 3: Rule-based fallback — absolute last resort when both LLM and ML are unavailable."""
    summary_lower = event_summary.lower()

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
        "reasoning": f"Tier 3 heuristic assessment (LLM and ML unavailable). Pattern-matched from event text.",
        "recommended_action": action,
        "estimated_recovery_probability": round(recovery_prob, 3),
        "llm_used": False,
        "model_used": "heuristic",
    }


def _template_message(
    event_type: str,
    customer_name: str,
    amount: float,
    failure_reason: str,
    channel: str,
) -> str:
    """Template-based message when LLM is unavailable. Includes Hinglish + voice templates."""
    name = customer_name.split()[0] if customer_name else "there"

    # Determine if Hinglish should be used
    use_hinglish = failure_reason in ("insufficient_funds", "invalid_otp") or channel == "voice"

    templates = {
        "payment_failure": {
            "sms": f"Hi {name}, your payment of INR {amount:,.0f} needs attention. Retry here: {{link}} - Payments Team",
            "email": f"Hi {name},\n\nYour recent payment of INR {amount:,.2f} couldn't be processed. Please retry using the link below.\n\nRetry: {{link}}\n\nRegards,\nPayments Team",
            "whatsapp": f"Hi {name}, your payment of INR {amount:,.0f} could not be processed. Please complete payment using this link: {{link}}",
            "voice": f"Namaste {name} ji, aapka INR {amount:,.0f} ka payment abhi process nahi ho paya. Koi baat nahi — aap is link se payment complete kar sakte hain. Dhanyavaad.",
        },
        "checkout_abandonment": {
            "sms": f"Hi {name}, you left items in your cart (INR {amount:,.0f}). Complete checkout: {{link}}",
            "email": f"Hi {name},\n\nLooks like you left some items in your cart worth INR {amount:,.2f}. Your cart is saved -- complete your purchase here.\n\nCheckout: {{link}}",
            "whatsapp": f"Hi {name}, your cart is saved with items worth INR {amount:,.0f}. Complete purchase here: {{link}}",
            "voice": f"Hello {name} ji, aapke cart mein INR {amount:,.0f} ke items saved hain. Aap abhi checkout complete kar sakte hain. Link aapko SMS pe bhej diya gaya hai.",
        },
        "subscription_failure": {
            "sms": f"Hi {name}, your subscription renewal of INR {amount:,.0f} needs attention. Update: {{link}}",
            "email": f"Hi {name},\n\nYour subscription renewal of INR {amount:,.2f} couldn't be processed. To avoid service interruption, please update your payment method.\n\nUpdate: {{link}}",
            "whatsapp": f"Hi {name}, your subscription renewal of INR {amount:,.0f} needs attention. Update payment method here: {{link}}",
            "voice": f"Namaste {name} ji, aapki subscription ka renewal INR {amount:,.0f} ka process nahi ho paya. Service bani rahe isliye payment update kar dijiye. Link aapke phone pe bhej diya hai.",
        },
        "receivable_overdue": {
            "sms": f"Reminder: Invoice of INR {amount:,.0f} is pending. Pay now: {{link}} - Accounts Team",
            "email": f"Dear {name},\n\nThis is a friendly reminder that invoice amount INR {amount:,.2f} is overdue. Please arrange payment at your earliest convenience.\n\nPay: {{link}}\n\nRegards,\nAccounts Receivable",
            "whatsapp": f"Hi {name}, invoice of INR {amount:,.0f} is pending. Please complete payment using this link: {{link}}",
            "voice": f"Namaste {name} ji, aapka INR {amount:,.0f} ka invoice abhi pending hai. Kripya jaldi se jaldi payment kar dijiye. Link SMS pe bhej diya gaya hai. Dhanyavaad.",
        },
    }

    # Hinglish override for payment failures with specific reasons
    if use_hinglish and channel in ("sms", "whatsapp"):
        hinglish_templates = {
            "payment_failure": {
                "sms": f"Hi {name}, aapka INR {amount:,.0f} ka payment process nahi hua. Yahan se retry karein: {{link}}",
                "whatsapp": f"Hi {name} ji, aapka INR {amount:,.0f} ka payment abhi process nahi ho paya. Koi baat nahi, is link se complete kar lijiye: {{link}}",
            },
            "checkout_abandonment": {
                "sms": f"Hi {name}, aapke cart mein INR {amount:,.0f} ke items hain. Checkout karein: {{link}}",
                "whatsapp": f"Hi {name} ji, aapka cart saved hai — INR {amount:,.0f}. Yahan se purchase complete karein: {{link}}",
            },
        }
        mode_key = _match_template_key(event_type, hinglish_templates)
        if mode_key and channel in hinglish_templates.get(mode_key, {}):
            return hinglish_templates[mode_key][channel]

    mode_key = _match_template_key(event_type, templates)
    channel_key = channel.lower()
    if channel_key not in ("sms", "email", "whatsapp", "voice"):
        channel_key = "sms"

    return templates.get(mode_key, templates["payment_failure"]).get(
        channel_key,
        f"Payment of ₹{amount:,.0f} needs attention. Link: {{link}}"
    )


def _match_template_key(event_type: str, templates: dict) -> str:
    """Match event_type string to template dictionary key."""
    mode_key = event_type.replace(".", "_").replace("charged_halted", "failure")
    for key in templates:
        if key in mode_key or mode_key in key:
            return key
    return "payment_failure"
