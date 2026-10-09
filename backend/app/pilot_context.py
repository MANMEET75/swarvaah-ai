"""Bounded, per-call synthetic scenarios for the local phone pilot."""

SAVED_REMINDER = "saved_reminder"
FESTIVAL_SALE_DEMO = "festival_sale_demo"


def greeting(name: str, language: str) -> str:
    if language == "hi-IN":
        return (
            f"नमस्ते {name}, मैं Swarvaah AI हूँ। यह Vijay Sales के Great Indian Festival sale "
            "के बारे में केवल एक काल्पनिक टेस्ट कॉल है। मैं Vijay Sales की ओर से नहीं बोल रही हूँ, "
            "और मेरे पास असली ऑफ़र की पुष्टि नहीं है। आप इस डेमो के बारे में क्या पूछना चाहेंगे?"
        )
    return (
        f"Hello {name}, I'm Swarvaah AI. This is only a synthetic test conversation about "
        "a Great Indian Festival sale scenario at Vijay Sales. I'm not calling on behalf of "
        "Vijay Sales, and I don't have verified offer details. What would you like to ask?"
    )


def fallback(language: str) -> str:
    if language == "hi-IN":
        return "यह सिर्फ एक टेस्ट है। मेरे पास असली सेल या ऑफ़र की पुष्टि नहीं है। क्या आप कुछ और पूछना चाहेंगे?"
    return "This is only a test. I don't have verified sale or offer details. Would you like to ask something else?"
