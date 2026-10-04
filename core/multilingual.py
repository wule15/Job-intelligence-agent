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

The module also reads which language an advert is written in, from its
common words (advert_language), for the filter that drops adverts in a
language the user cannot read. See the section at the end.
"""

import re
import unicodedata
from collections import Counter


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


# ── The language an advert is written in ─────────────────────────────────────
# Read from its common words: articles, prepositions, pronouns and the like,
# which every advert in a language uses whatever the job. Each list holds
# words that are frequent in its language and rare in the others, written
# folded (lower case, no accents). Words many languages share ("na", "de",
# "en", "in") are left out. The South Slavic list keeps "je", "se", "sa" and
# "ste" on purpose, though Slovak, Czech and Slovenian use them too, so a
# Serbian, Croatian or Bosnian advert is never read as one of those. The cost
# falls the safe way: a Slovak advert needs a clearer lead to be recognised.
ADVERT_LANGUAGE_WORDS = {
    'english': 'the and of is you with our we will are your this that have from for be '
               'they which who can it',
    'german': 'der die das und ist mit fur sie wir ein eine einen einer im zu auf den dem '
              'von nicht oder auch bei ihre ihr uns sind werden wird sich zur zum uber',
    'dutch': 'het een van voor met wij zijn niet ook bij naar onze jouw ons wordt worden '
             'aan deze jij uit dat op heb hebt kun kunt',
    'french': 'les des et est une pour dans avec vous nous aux votre notre sont etre '
              'cette au sur par leur nos vos chez le qui ou',
    'italian': 'il di che per una del della delle dei con sono nel nella alla gli essere '
               'siamo nostro nostra anche',
    'spanish': 'el los las por para y es su sus como nuestro nuestra somos buscamos',
    'portuguese': 'os das dos em com uma nao voce nossa nosso sao ser mais ao aos pelo pela',
    'polish': 'w sie oraz dla jest nie lub przez jestes bedziesz oferujemy pracy naszej '
              'naszym wymagania',
    'czech': 'pro jako jsme jste nebo take jsou nase bude ktery ktera ktere kteri ve '
             'pokud v z pri ze mate',
    'slovak': 'pre sme alebo aj vo ktory ktora ktore ktori ktorych ak tiez v z pri ze '
              'mate mame',
    'slovenian': 'ki tudi bo kot so ter lahko',
    'hungarian': 'az es hogy egy meg vagy valamint szamara kell nem mint',
    'romanian': 'si pe cu care este pentru ale din sau sunt',
    'turkish': 've bir bu ile icin olarak olan gibi veya cok daha sahip ilgili',
    'finnish': 'ja on ei tai etta seka kanssa joka jotka olet meilla sinulla myos kuin '
               'ovat voit',
    'danish': 'og af av ikke vores hos eller skal til bliver dig din dine med som har vi',
    'swedish': 'och att ar ett till inte eller kommer hos med som har vi',
    'serbian': 'i u da su kao ili koji koja koje kojih kojem smo biti ce sto kako nije iz '
               'uz kroz ukoliko je se sa ste nudimo te',
}
_ADVERT_WORD_SETS = {lang: frozenset(words.split())
                     for lang, words in ADVERT_LANGUAGE_WORDS.items()}
# Letters only some languages write. A word holding one counts as a common
# word of those languages, read before the accents are removed. They matter
# most for Slovak and Czech, whose common words are mostly shared with
# Serbian ("na", "sa", "je") or with English ("a"): Serbian, Croatian and
# Bosnian never write ľ, ř, ť or ý, so these letters can only push a reading
# away from them.
ADVERT_LANGUAGE_LETTERS = {
    'slovak': 'ľĺŕťďňý',
    'czech': 'řůěťďňý',
    'polish': 'łąęśźż',
    'serbian': 'đ',
    'hungarian': 'őű',
    'romanian': 'ășțţ',
    'turkish': 'ğı',
    'spanish': 'ñ',
    'portuguese': 'ãõ',
    'danish': 'æø',
    'german': 'ß',
    'french': 'œ',
}
# Close relatives that share many common words. The lead is measured against
# languages outside the family, and the family member with the most hits is
# the reading, so a Slovak advert is not left unread for looking a bit Czech.
ADVERT_LANGUAGE_FAMILIES = ({'czech', 'slovak'}, {'danish', 'swedish'})
_FAMILY = {lang: frozenset(family) for family in ADVERT_LANGUAGE_FAMILIES
           for lang in family}
# Other names a user may give a language, mapped to the list that reads it.
ADVERT_LANGUAGE_ALIASES = {
    'croatian': 'serbian', 'bosnian': 'serbian', 'montenegrin': 'serbian',
    'serbo-croatian': 'serbian', 'norwegian': 'danish',
}
# A script stands in for the words where a language has its own: Hangul is
# Korean, kana is Japanese. Cyrillic is not read at all, because Serbian is
# written in it as well as Russian, Bulgarian and Macedonian.
_SCRIPTS = (
    ('korean', ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ('japanese', ((0x3040, 0x30FF),)),
    ('arabic', ((0x0600, 0x06FF), (0x0750, 0x077F))),
    ('greek', ((0x0370, 0x03FF),)),
    ('hebrew', ((0x0590, 0x05FF),)),
    ('thai', ((0x0E00, 0x0E7F),)),
    ('chinese', ((0x4E00, 0x9FFF),)),
)
# How sure the reading must be. A body under ADVERT_MIN_WORDS words, such as
# a teaser, is not judged. The reading must account for at least
# ADVERT_MIN_SHARE of all the words, and outnumber every unrelated language
# ADVERT_LEAD times over, English included, so an English advert with a
# paragraph in another language reads as English.
#
# Checked on a live batch of 10,670 adverts, October 2026: 7,439 read as
# English, 1,527 were not judged, and the rest read as 17 other languages.
# Set against each job's location, the only readings a location contradicts
# are German adverts for jobs abroad and one Slovak advert read as Czech. 40
# full adverts from the local board read as English or Serbian, none as
# anything else. About a millisecond per advert.
ADVERT_MIN_WORDS = 40
ADVERT_MIN_SHARE = 0.05
ADVERT_LEAD = 2.0
_WORD = re.compile(r'[^\W\d_]+')


def canonical_language(name):
    """The list name for a language the user wrote, aliases resolved."""
    folded = fold_diacritics(name).strip()
    return ADVERT_LANGUAGE_ALIASES.get(folded, folded)


_LETTER = re.compile(r'[^\W\d_]')
_NON_ASCII_LETTER = re.compile(r'[^\W\d_a-zA-Z]')
_SCRIPT_PATTERNS = tuple(
    (lang, re.compile('[' + ''.join(f'{chr(lo)}-{chr(hi)}' for lo, hi in ranges) + ']'))
    for lang, ranges in _SCRIPTS)


def _script_language(text):
    """The language of a script that is most of the letters, or None."""
    letters = len(_LETTER.findall(text))
    if letters < ADVERT_MIN_WORDS or 2 * len(_NON_ASCII_LETTER.findall(text)) < letters:
        return None
    for lang, pattern in _SCRIPT_PATTERNS:
        if 2 * len(pattern.findall(text)) >= letters:
            return lang
    return None


# The same lists turned round, for speed: a word, or a letter, to the
# languages it belongs to.
_LANGS_OF_WORD = {}
for _lang, _vocab in _ADVERT_WORD_SETS.items():
    for _word in _vocab:
        _LANGS_OF_WORD.setdefault(_word, set()).add(_lang)
_LANGS_OF_LETTER = {}
for _lang, _letters in ADVERT_LANGUAGE_LETTERS.items():
    for _letter in _letters:
        _LANGS_OF_LETTER.setdefault(_letter, set()).add(_lang)


def _word_counts(text):
    """(hits per language, number of words), or None when too short. A hit
    is a common word of the language, or a word holding one of its letters;
    a word counts at most once for each language."""
    raw = _WORD.findall((text or '').lower())
    if len(raw) < ADVERT_MIN_WORDS:
        return None
    counts = dict.fromkeys(_ADVERT_WORD_SETS, 0)
    for word, n in Counter(raw).items():
        langs = set(_LANGS_OF_WORD.get(fold_diacritics(word), ()))
        if not word.isascii():
            for letter in set(word):
                langs.update(_LANGS_OF_LETTER.get(letter, ()))
        for lang in langs:
            counts[lang] += n
    return counts, len(raw)


def advert_language(text):
    """
    The language an advert is written in, as an English name, or None when
    the text is too short, written in Cyrillic, or not clearly one language.
    Serbian, Croatian and Bosnian all read as 'serbian', and Norwegian as
    'danish'.
    """
    if not text:
        return None
    script = _script_language(text)
    if script:
        return script
    measured = _word_counts(text)
    if not measured:
        return None
    counts, total = measured
    best = max(counts, key=counts.get)
    family = _FAMILY.get(best, {best})
    rest = max(n for lang, n in counts.items() if lang not in family)
    if counts[best] < ADVERT_MIN_SHARE * total or counts[best] < ADVERT_LEAD * rest:
        return None
    return best


def written_in(text, languages):
    """True when the advert reads as one of `languages`, a set of list names
    (see canonical_language). English is never one of them."""
    return advert_language(text) in set(languages) - {'english', None}
