"""Bounded Actnoww subscription conversation for an opted-in self-call pilot."""

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AuditEvent, CallAttempt, CallEvent, CallSession, Contact

SCENARIO = "actnoww_subscription"
SCRIPT = "actnoww_subscription_v1"
CAMPAIGN_NAME = "Actnoww Kids Learning Subscription"

# These are the only product claims supplied and approved for this campaign.
APPROVED_FACTS = (
    "Actnoww is a kids' educational learning app. "
    "It helps young children learn ABCs and early concepts through engaging learning activities. "
    "The operator states that one course costs ₹999. The course name, included content, duration, "
    "trial, offers, taxes, refund terms, and checkout process have not been supplied."
)

OPT_OUT = re.compile(r"\b(?:do not call|don't call|stop calling|unsubscribe|opt out)\b|कॉल मत|फ़ोन मत|फोन मत", re.I)
DECLINE = re.compile(r"\b(?:not interested|no thanks|no thank you|don't want|do not want)\b|दिलचस्पी नहीं|रुचि नहीं", re.I)
BUSY = re.compile(r"\b(?:busy|call (?:me )?(?:back|later)|not a good time|later today|tomorrow)\b|बाद में|अभी नहीं", re.I)
BUY = re.compile(
    r"\b(?:sign me up|subscribe me|i (?:want|would like|am ready) to (?:subscribe|sign up|buy|purchase)|"
    r"i(?:'ll| will) (?:subscribe|buy|purchase)|i(?:'ll| will) take the plan|let's subscribe)\b|"
    r"(?:मुझे|मैं)\s*(?:सब्सक्राइब|खरीद)", re.I)
INFO = re.compile(r"\b(?:send (?:me )?(?:info|details|information)|more information|send a link)\b|जानकारी भेज", re.I)
INTEREST = re.compile(r"\b(?:interested|sounds good|tell me more|looks good)\b|दिलचस्पी है", re.I)


def greeting(name: str, language: str) -> str:
    if language == "hi-IN":
        return (f"नमस्ते {name}, मैं Actnoww की AI assistant बोल रही हूँ। "
                "Actnoww छोटे बच्चों के लिए एक learning app है। क्या अभी एक मिनट बात कर सकते हैं?")
    return (f"Hello {name}, I'm an AI assistant calling on behalf of Actnoww, "
            "a learning app for young children. Is now a good time for a quick chat?")


def fallback(language: str) -> str:
    if language == "hi-IN":
        return ("Actnoww बच्चों को ABCs और शुरुआती चीज़ें engaging activities से सीखने में मदद करता है। "
                "आपके बच्चे को अभी क्या सीखना अच्छा लगता है?")
    return ("Actnoww helps young children learn ABCs and early concepts through engaging activities. "
            "What is your child learning at the moment?")


def apply_policy(db: Session, session: CallSession, contact: Contact, text: str) -> dict | None:
    """Commit only explicit caller intent; never imply a paid subscription happened."""
    message = text.strip()
    outcome = None
    action = None
    ended = False
    hindi = session.language == "hi-IN" or bool(re.search(r"[\u0900-\u097f]", message))
    if OPT_OUT.search(message):
        outcome, action, ended = "do_not_call", "opt_out", True
        reply = ("समझ गई। हम आपको दोबारा कॉल नहीं करेंगे। धन्यवाद, नमस्ते।" if hindi else
                 "Understood. We will not call you again. Thank you, goodbye.")
        contact.suppressed = True
        db.add(AuditEvent(action="contact.opt_out", target_id=contact.id, detail="Actnoww voice session"))
    elif DECLINE.search(message):
        outcome, ended = "not_interested", True
        reply = ("बिलकुल। समय देने के लिए धन्यवाद। आपका दिन शुभ हो।" if hindi else
                 "Of course. Thank you for your time, and have a good day.")
    elif BUSY.search(message):
        outcome, action, ended = "callback_requested", "callback_request", True
        reply = ("कोई बात नहीं। मैंने callback का अनुरोध नोट कर लिया है। हमारी टीम समय की पुष्टि करेगी। धन्यवाद, नमस्ते।" if hindi else
                 "No problem. I've noted that you'd prefer a callback. Our team will confirm a time. Thank you, goodbye.")
    elif BUY.search(message):
        outcome, action, ended = "subscription_requested", "subscription_request", True
        reply = (("रुचि के लिए धन्यवाद। एक course की कीमत ₹999 है। मैंने subscription का अनुरोध दर्ज किया है; "
                  "अभी कोई payment या subscription पूरी नहीं हुई है। हमारी टीम signup के बारे में बता सकती है। नमस्ते।") if hindi else
                 ("Thanks for your interest. One course is ₹999. I've recorded your subscription request, "
                  "but no payment or subscription has been completed. Our team can explain the approved signup process. Goodbye."))
    elif INFO.search(message):
        outcome, action, ended = "information_requested", "information_request", True
        reply = (("मैंने जानकारी का आपका अनुरोध नोट कर लिया है। अभी मेरे पास भेजने के लिए approved link नहीं है। "
                  "हमारी टीम आपसे संपर्क कर सकती है। धन्यवाद, नमस्ते।") if hindi else
                 ("I've noted your request for information. I don't have an approved link to send right now; "
                  "our team can follow up. Thank you, goodbye."))
    elif INTEREST.search(message):
        outcome = "interested"
        reply = ("अच्छा लगा सुनकर। आपका बच्चा लगभग किस उम्र का है, और अभी क्या सीख रहा है?" if hindi else
                 "Glad to hear that. What age range is your child in, and what are they learning at the moment?")
    else:
        return None
    db.add(CallEvent(session_id=session.id, kind="caller", text=text))
    db.add(CallEvent(session_id=session.id, kind="assistant", text=reply))
    if action:
        db.add(CallEvent(session_id=session.id, kind="action", text=action))
    session.outcome = outcome
    session.workflow_state = "ended" if ended else "awaiting_intent"
    if ended:
        session.status = "ended"
        session.ended_at = datetime.now(timezone.utc)
        if session.attempt_id:
            attempt = db.get(CallAttempt, session.attempt_id)
            if attempt:
                attempt.status = "completed"
    db.commit()
    return {"reply": reply, "state": session.workflow_state, "outcome": outcome, "action": action, "model": None}
