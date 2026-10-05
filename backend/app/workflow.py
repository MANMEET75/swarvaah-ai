"""Deterministic reminder dialogue; business effects are explicit events."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Turn:
    state: str
    outcome: str | None
    reply: str
    action: str | None = None


YES = re.compile(r"^(?:yes|yeah|yep|confirm|confirmed|okay|ok|sure|haan|han|ha)\b|^(?:हाँ|ठीक|पक्का)(?:\s|$)|\b(?:i confirm|please confirm|i will attend|i'll be there|i will be there)\b", re.I)
NO = re.compile(r"^(?:no|not now|cancel|nahi|nahin)\b|^नहीं(?:\s|$)|\b(?:i cannot attend|i can't attend|please cancel)\b", re.I)
RESCHEDULE = re.compile(r"\b(reschedule|change|another time|later|postpone|समय बदल|बाद में)\b", re.I)
HUMAN = re.compile(r"\b(agent|human|person|representative|किसी से बात|इंसान)\b", re.I)
STOP = re.compile(r"\b(stop calling|do not call|don't call|opt out|unsubscribe|कॉल मत|बंद करो)\b", re.I)
INFORMATIONAL = re.compile(r"^(?:what (?:does|is)|why|how (?:does|do|can)|explain|tell me|could you explain|can you explain)\b", re.I)


def greeting(name: str, label: str, when: str, language: str) -> str:
    if language == "hi-IN":
        return f"नमस्ते {name}, मैं Swarvaah AI assistant बोल रही हूँ। यह {label} के लिए {when} का service reminder है। क्या आप इसे confirm करना चाहेंगे?"
    return f"Hello {name}, I'm the Swarvaah AI assistant. This is a service reminder for {label} {when}. Would you like to confirm it?"


def respond(state: str, text: str, language: str = "en-IN") -> Turn:
    message = text.strip()
    hindi = language == "hi-IN" or bool(re.search(r"[\u0900-\u097f]", message))
    if INFORMATIONAL.search(message):
        return Turn("awaiting_intent", None, "ज़रूर, मैं समझा सकती हूँ। आप क्या जानना चाहेंगे?" if hindi else "Of course. What would you like to know?")
    if STOP.search(message) or "कॉल मत" in message or "बंद करो" in message:
        return Turn("ended", "opted_out", "ठीक है, हम आपको दोबारा कॉल नहीं करेंगे। बताने के लिए धन्यवाद। नमस्ते।" if hindi else "Understood. We will not call you again. Thank you for letting me know. Goodbye.", "opt_out")
    if HUMAN.search(message) or "किसी से बात" in message:
        return Turn("ended", "human_callback", "ज़रूर। हमारी टीम आपसे संपर्क करेगी। धन्यवाद, आपका दिन शुभ हो।" if hindi else "Of course. A person from our team will contact you. Thank you, and have a good day.", "human_callback")
    if RESCHEDULE.search(message) or "समय बदल" in message or "बाद में" in message:
        return Turn("ended", "reschedule_requested", "समय बदलने का अनुरोध दर्ज कर लिया है। समय अभी बदला नहीं है; हमारी टीम उपलब्ध समय की पुष्टि करेगी। धन्यवाद, आपका दिन शुभ हो।" if hindi else "I've noted your request to reschedule. The time has not changed yet; our team will confirm an available slot. Thank you, and have a good day.", "reschedule_request")
    if YES.search(message) and not re.search(r"\b(?:but|maybe|not sure)\b", message, re.I):
        return Turn("ended", "confirmed", "धन्यवाद, आपका confirmation दर्ज हो गया है। आपका दिन शुभ हो।" if hindi else "Thank you. Your confirmation has been recorded. Have a good day.", "confirm")
    if NO.search(message):
        return Turn("ended", "declined", "समझ गई। हम इसे confirm नहीं करेंगे। धन्यवाद, आपका दिन शुभ हो।" if hindi else "Understood. I won't mark it as confirmed. Thank you for your time. Goodbye.", "decline")
    if state == "ended":
        return Turn("ended", None, "यह बातचीत समाप्त हो गई है।" if hindi else "This conversation has ended.")
    return Turn("awaiting_intent", None, "क्या आप confirm, reschedule, या किसी व्यक्ति से बात करना चाहेंगे?" if hindi else "Would you like to confirm, request a new time, or speak with a person?")
