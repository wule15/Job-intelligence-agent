"""
English equivalents for local-language job adverts.

The scorer matches the CV's English skill words against the advert. A Serbian
or German advert for the same job matched almost nothing, so it sank below the
score cutoff whatever it said. This glossary finds known local terms and
returns their English equivalents, which the scorer reads alongside the
original text. The advert itself is never changed, and English text gains
nothing, so English scores stay exactly as they were.

Patterns are regular expressions matched against the folded text: lowercase,
accents removed, đ written as dj. Serbian and Croatian patterns are anchored
at the start of a word and list their endings, because a bare stem catches the
wrong word ("ventil" is also the start of "ventilacija", ventilation). German
patterns are not anchored, because German builds compounds: "armatur" has to
match inside "Industriearmaturen".

Add entries freely. Keep each English side to words the CV actually uses.
"""

import re
import unicodedata


def fold_diacritics(text: str) -> str:
    """Lowercase and strip accents, so the same word matches written either way.

    NFKD splits a letter from its accent, and the accent is dropped. The
    letter đ has no such split, so it is mapped to "dj", its usual plain form.
    """
    text = (text or '').lower().replace('đ', 'dj')
    return ''.join(c for c in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(c))


# ── Serbian, Croatian, Bosnian ───────────────────────────────────────────────
_SOUTH_SLAVIC = [
    (r'\binzenjer\w* (primene i )?prodaj', 'technical sales pre-sales sales engineer'),
    (r'\btehnick\w* prodaj', 'technical sales technical sales support'),
    (r'\bprodaj\w*', 'sales'),
    (r'\bkomercijalist\w*', 'sales client qualification'),
    (r'\bkljucn\w* kup(ac|c)\w*', 'key account stakeholder management'),
    (r'\bklijent\w*', 'stakeholder management'),
    (r'\bventil(a|i|e|om|ima)?\b', 'industrial valves flow control valve'),
    (r'\bpump(a|e|i|u|om|ama)?\b', 'pump fluid systems'),
    (r'\bhidraulik\w*', 'hydraulics fluid systems'),
    (r'\bpneumatik\w*', 'pneumatics fluid systems'),
    (r'\bpreventivn\w* odrzavanj\w*', 'preventive maintenance asset management'),
    (r'\bodrzavanj\w*', 'maintenance asset management'),
    (r'\bpustanj\w* u (rad|pogon)', 'commissioning'),
    (r'\bautomatizacij\w*', 'automation industrial automation'),
    (r'\bprocesn\w*', 'process engineering process industry process equipment'),
    (r'\bmasinsk\w*', 'mechanical engineering'),
    (r'\bprojektovanj\w*', 'engineering design solution design'),
    (r'\b(upravljanj|vodjenj)\w* projekt\w*', 'project management'),
    (r'\btehnick\w* dokumentacij\w*', 'technical documentation project documentation'),
    (r'\bizrad\w* ponud\w*', 'proposal writing'),
    (r'\bponud(a|e|i|u|ama)?\b', 'proposal'),
    (r'\b(tender\w*|javn\w* nabavk\w*)', 'tender response RFP response bid coordination'),
    (r'\bnabavk\w*', 'procurement supplier evaluation'),
    (r'\bspecifikacij\w*', 'specification analysis'),
    (r'\bprezentacij\w*', 'presentations stakeholder presentations'),
    (r'\bnaft\w* i gas\w*', 'oil and gas'),
    (r'\brudar\w*', 'mining'),
    (r'\bkvalitet\w*', 'quality assurance'),
    (r'\b(ispitivanj|testiranj)\w*', 'testing'),
    (r'\bmerenj\w*', 'measurement'),
    (r'\binstrumentacij\w*', 'instrumentation'),
    (r'\bkalibracij\w*', 'calibration'),
    (r'\b(grejanj|hladjenj|termotehnik)\w*', 'thermal engineering heat transfer'),
    (r'\bpregovar\w*', 'negotiation'),
]

# ── German ────────────────────────────────────────────────────────────────────
_GERMAN = [
    (r'technisch\w* vertrieb', 'technical sales pre-sales'),
    (r'vertriebsingenieur', 'technical sales pre-sales sales engineer'),
    (r'vertrieb', 'sales'),
    (r'anwendungstechni', 'application engineer technical sales support'),
    (r'armatur', 'industrial valves flow control valve'),
    (r'\bventil(e|en)?\b', 'industrial valves flow control valve'),
    (r'pumpe', 'pump fluid systems'),
    (r'hydraulik', 'hydraulics fluid systems'),
    (r'pneumatik', 'pneumatics fluid systems'),
    (r'(instandhaltung|wartung)', 'maintenance asset management'),
    (r'inbetriebnahme', 'commissioning'),
    (r'automatisierung', 'automation industrial automation'),
    (r'verfahrenstechni', 'process engineering process industry process equipment'),
    (r'maschinenbau', 'mechanical engineering'),
    (r'(projektleit|projektmanagement)', 'project management'),
    (r'technische\w* dokumentation', 'technical documentation project documentation'),
    (r'angebotserstell', 'proposal writing'),
    (r'angebot', 'proposal'),
    (r'ausschreibung', 'tender response RFP response bid coordination'),
    (r'(einkauf|beschaffung)', 'procurement supplier evaluation'),
    (r'kundenbetreuung', 'stakeholder management'),
    (r'praesentation|prasentation', 'presentations stakeholder presentations'),
    (r'qualitatssicherung', 'quality assurance'),
    (r'(prufung|pruefung)', 'testing'),
    (r'messtechni', 'measurement instrumentation'),
    (r'kalibrier', 'calibration'),
]

GLOSSARY = [(re.compile(p), en) for p, en in _SOUTH_SLAVIC + _GERMAN]


def english_equivalents(text: str) -> str:
    """English equivalents of the local terms found in text, space separated.

    Each equivalent appears once however often its term occurs, so a long
    advert that repeats a word is not scored higher for repeating it.
    Returns '' when nothing matches.
    """
    folded = fold_diacritics(text)
    found = []
    for pattern, english in GLOSSARY:
        if english not in found and pattern.search(folded):
            found.append(english)
    return ' '.join(found)
