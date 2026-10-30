"""Turn what speech recognition heard into the canonical names used in clinics.json.

Speech-to-text often mishears short brand names ("Aetna" -> "athena"). Each lookup
tries, in order: exact canonical name, alias table, fuzzy match. It returns the
canonical name, or "" when nothing is close enough. The caller decides what to do
with "" (usually: ask the caller to repeat or spell it).

To support a new insurer or language, add it to the tables below. Keep every alias
lowercase.
"""

import difflib
import re

INSURANCES = ["Aetna", "Cigna", "UnitedHealthcare", "Medicaid", "Medicare", "Oscar", "Self-pay"]

INSURANCE_ALIASES = {
    # Aetna
    "athena": "Aetna", "etna": "Aetna", "edna": "Aetna", "aetna health": "Aetna", "a etna": "Aetna",
    # Cigna
    "signa": "Cigna", "sigma": "Cigna", "cygna": "Cigna", "cigna health": "Cigna",
    # UnitedHealthcare
    "united": "UnitedHealthcare", "united health": "UnitedHealthcare", "uhc": "UnitedHealthcare",
    "united healthcare": "UnitedHealthcare", "united health care": "UnitedHealthcare",
    # Medicaid / Medicare
    "medic aid": "Medicaid", "medicate": "Medicaid", "medi-cal": "Medicaid",
    "medic air": "Medicare", "medi care": "Medicare",
    # Oscar
    "oscar health": "Oscar",
    # Self-pay
    "self pay": "Self-pay", "selfpay": "Self-pay", "cash": "Self-pay", "out of pocket": "Self-pay",
    "no insurance": "Self-pay", "none": "Self-pay", "自费": "Self-pay",
}

LANGUAGES = ["Mandarin", "Cantonese", "English", "Spanish", "Korean"]

LANGUAGE_ALIASES = {
    "chinese": "Mandarin", "mandarin chinese": "Mandarin", "putonghua": "Mandarin",
    "普通话": "Mandarin", "中文": "Mandarin", "国语": "Mandarin", "汉语": "Mandarin",
    "粤语": "Cantonese", "广东话": "Cantonese",
    "espanol": "Spanish", "español": "Spanish", "西班牙语": "Spanish",
    "英语": "English", "英文": "English",
    "韩语": "Korean", "한국어": "Korean",
}

MODE_ALIASES = {
    "in_person": "in_person", "inperson": "in_person", "in person": "in_person", "in-person": "in_person",
    "offline": "in_person", "onsite": "in_person", "in office": "in_person", "线下": "in_person",
    "online": "online", "virtual": "online", "video": "online", "telehealth": "online",
    "remote": "online", "zoom": "online", "线上": "online",
}

FUZZY_CUTOFF = 0.72


def _key(s):
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _resolve(raw, canonical, aliases):
    k = _key(raw)
    if not k:
        return ""
    by_lower = {c.lower(): c for c in canonical}
    if k in by_lower:
        return by_lower[k]
    if k in aliases:
        return aliases[k]
    # A full plan name that contains a known name: "Aetna Better Health" -> Aetna
    words = f" {k} "
    for low, canon in by_lower.items():
        if f" {low} " in words:
            return canon
    candidates = list(by_lower) + list(aliases)
    hit = difflib.get_close_matches(k, candidates, n=1, cutoff=FUZZY_CUTOFF)
    if not hit:
        return ""
    return by_lower.get(hit[0]) or aliases[hit[0]]


def insurance(raw):
    return _resolve(raw, INSURANCES, INSURANCE_ALIASES)


def language(raw):
    return _resolve(raw, LANGUAGES, LANGUAGE_ALIASES)


def mode(raw):
    k = _key(raw).replace("_", " ")
    if not k:
        return ""
    return MODE_ALIASES.get(k) or MODE_ALIASES.get(k.replace(" ", "_")) or ""
