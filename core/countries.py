"""
Which countries a job's location field names, read without a network call.

The country allow-list in core/job_filter.py needs to know where a job is. The
location field is the only reliable place to look: an advert's description
mentions offices, customers and markets that say nothing about where the job
itself sits. Location fields come in many shapes ("Kurli, MH, India",
"Austin, TX", "Remote - EMEA", "Munich (Germany)"), so this module turns one
into a set of ISO 3166 country codes.

An empty set means the location could not be placed ("Remote", "2 Locations",
a bare city name). The caller must treat that as unknown, not as foreign.

Rules, in the order they matter:
  - Names are matched as whole words after accents are removed, longest
    first, so "New Mexico" is the US state and not Mexico, "Indiana" is not
    India, and "Dominican Republic" is not Dominica.
  - US state names count as the United States. A two-letter US state code
    after a comma ("Austin, TX") counts only when written in capitals and
    when nothing after it names another country ("Almere, FL, NL" is in the
    Netherlands). A code the caller protects (because it is also an allowed
    country's code, such as DE for Germany and Delaware) is left unresolved.
  - An upper-case ISO code that ends the location after a region, or comes
    before a postcode, is that country ("Munich, BY, DE", "Pune, IN, 411013").
  - Region words give tokens instead of codes: "EU" for the European Union
    or the EEA, "EUROPE" for Europe-wide wording such as Europe or EMEA,
    "OTHER_REGION" for other continents (APAC, LATAM, AMER, North America),
    and "WORLDWIDE" for worldwide, global or anywhere.
  - A short list of large job hubs written without a country ("San
    Francisco - SF9", "King Abdullah Economic City, 02 (02)") places the job
    in that hub's country, see CITY_COUNTRIES.

Standard library only.
"""

import re
from urllib.parse import urlparse

from core.multilingual import fold_diacritics

# ISO 3166-1 alpha-2 codes and short English names, plus Kosovo (XK), which
# has no official code but is used by job boards and international bodies.
COUNTRY_NAMES = {
    'AD': 'Andorra', 'AE': 'United Arab Emirates', 'AF': 'Afghanistan',
    'AG': 'Antigua and Barbuda', 'AI': 'Anguilla', 'AL': 'Albania',
    'AM': 'Armenia', 'AO': 'Angola', 'AQ': 'Antarctica', 'AR': 'Argentina',
    'AS': 'American Samoa', 'AT': 'Austria', 'AU': 'Australia', 'AW': 'Aruba',
    'AX': 'Aland Islands', 'AZ': 'Azerbaijan', 'BA': 'Bosnia and Herzegovina',
    'BB': 'Barbados', 'BD': 'Bangladesh', 'BE': 'Belgium', 'BF': 'Burkina Faso',
    'BG': 'Bulgaria', 'BH': 'Bahrain', 'BI': 'Burundi', 'BJ': 'Benin',
    'BL': 'Saint Barthelemy', 'BM': 'Bermuda', 'BN': 'Brunei', 'BO': 'Bolivia',
    'BQ': 'Caribbean Netherlands', 'BR': 'Brazil', 'BS': 'Bahamas',
    'BT': 'Bhutan', 'BV': 'Bouvet Island', 'BW': 'Botswana', 'BY': 'Belarus',
    'BZ': 'Belize', 'CA': 'Canada', 'CC': 'Cocos Islands',
    'CD': 'Democratic Republic of the Congo', 'CF': 'Central African Republic',
    'CG': 'Republic of the Congo', 'CH': 'Switzerland', 'CI': "Cote d'Ivoire",
    'CK': 'Cook Islands', 'CL': 'Chile', 'CM': 'Cameroon', 'CN': 'China',
    'CO': 'Colombia', 'CR': 'Costa Rica', 'CU': 'Cuba', 'CV': 'Cabo Verde',
    'CW': 'Curacao', 'CX': 'Christmas Island', 'CY': 'Cyprus', 'CZ': 'Czechia',
    'DE': 'Germany', 'DJ': 'Djibouti', 'DK': 'Denmark', 'DM': 'Dominica',
    'DO': 'Dominican Republic', 'DZ': 'Algeria', 'EC': 'Ecuador',
    'EE': 'Estonia', 'EG': 'Egypt', 'EH': 'Western Sahara', 'ER': 'Eritrea',
    'ES': 'Spain', 'ET': 'Ethiopia', 'FI': 'Finland', 'FJ': 'Fiji',
    'FK': 'Falkland Islands', 'FM': 'Micronesia', 'FO': 'Faroe Islands',
    'FR': 'France', 'GA': 'Gabon', 'GB': 'United Kingdom', 'GD': 'Grenada',
    'GE': 'Georgia', 'GF': 'French Guiana', 'GG': 'Guernsey', 'GH': 'Ghana',
    'GI': 'Gibraltar', 'GL': 'Greenland', 'GM': 'Gambia', 'GN': 'Guinea',
    'GP': 'Guadeloupe', 'GQ': 'Equatorial Guinea', 'GR': 'Greece',
    'GS': 'South Georgia and the South Sandwich Islands', 'GT': 'Guatemala',
    'GU': 'Guam', 'GW': 'Guinea-Bissau', 'GY': 'Guyana', 'HK': 'Hong Kong',
    'HM': 'Heard Island and McDonald Islands', 'HN': 'Honduras',
    'HR': 'Croatia', 'HT': 'Haiti', 'HU': 'Hungary', 'ID': 'Indonesia',
    'IE': 'Ireland', 'IL': 'Israel', 'IM': 'Isle of Man', 'IN': 'India',
    'IO': 'British Indian Ocean Territory', 'IQ': 'Iraq', 'IR': 'Iran',
    'IS': 'Iceland', 'IT': 'Italy', 'JE': 'Jersey', 'JM': 'Jamaica',
    'JO': 'Jordan', 'JP': 'Japan', 'KE': 'Kenya', 'KG': 'Kyrgyzstan',
    'KH': 'Cambodia', 'KI': 'Kiribati', 'KM': 'Comoros',
    'KN': 'Saint Kitts and Nevis', 'KP': 'North Korea', 'KR': 'South Korea',
    'KW': 'Kuwait', 'KY': 'Cayman Islands', 'KZ': 'Kazakhstan', 'LA': 'Laos',
    'LB': 'Lebanon', 'LC': 'Saint Lucia', 'LI': 'Liechtenstein',
    'LK': 'Sri Lanka', 'LR': 'Liberia', 'LS': 'Lesotho', 'LT': 'Lithuania',
    'LU': 'Luxembourg', 'LV': 'Latvia', 'LY': 'Libya', 'MA': 'Morocco',
    'MC': 'Monaco', 'MD': 'Moldova', 'ME': 'Montenegro', 'MF': 'Saint Martin',
    'MG': 'Madagascar', 'MH': 'Marshall Islands', 'MK': 'North Macedonia',
    'ML': 'Mali', 'MM': 'Myanmar', 'MN': 'Mongolia', 'MO': 'Macao',
    'MP': 'Northern Mariana Islands', 'MQ': 'Martinique', 'MR': 'Mauritania',
    'MS': 'Montserrat', 'MT': 'Malta', 'MU': 'Mauritius', 'MV': 'Maldives',
    'MW': 'Malawi', 'MX': 'Mexico', 'MY': 'Malaysia', 'MZ': 'Mozambique',
    'NA': 'Namibia', 'NC': 'New Caledonia', 'NE': 'Niger',
    'NF': 'Norfolk Island', 'NG': 'Nigeria', 'NI': 'Nicaragua',
    'NL': 'Netherlands', 'NO': 'Norway', 'NP': 'Nepal', 'NR': 'Nauru',
    'NU': 'Niue', 'NZ': 'New Zealand', 'OM': 'Oman', 'PA': 'Panama',
    'PE': 'Peru', 'PF': 'French Polynesia', 'PG': 'Papua New Guinea',
    'PH': 'Philippines', 'PK': 'Pakistan', 'PL': 'Poland',
    'PM': 'Saint Pierre and Miquelon', 'PN': 'Pitcairn', 'PR': 'Puerto Rico',
    'PS': 'Palestine', 'PT': 'Portugal', 'PW': 'Palau', 'PY': 'Paraguay',
    'QA': 'Qatar', 'RE': 'Reunion', 'RO': 'Romania', 'RS': 'Serbia',
    'RU': 'Russia', 'RW': 'Rwanda', 'SA': 'Saudi Arabia',
    'SB': 'Solomon Islands', 'SC': 'Seychelles', 'SD': 'Sudan', 'SE': 'Sweden',
    'SG': 'Singapore', 'SH': 'Saint Helena', 'SI': 'Slovenia',
    'SJ': 'Svalbard and Jan Mayen', 'SK': 'Slovakia', 'SL': 'Sierra Leone',
    'SM': 'San Marino', 'SN': 'Senegal', 'SO': 'Somalia', 'SR': 'Suriname',
    'SS': 'South Sudan', 'ST': 'Sao Tome and Principe', 'SV': 'El Salvador',
    'SX': 'Sint Maarten', 'SY': 'Syria', 'SZ': 'Eswatini',
    'TC': 'Turks and Caicos Islands', 'TD': 'Chad',
    'TF': 'French Southern Territories', 'TG': 'Togo', 'TH': 'Thailand',
    'TJ': 'Tajikistan', 'TK': 'Tokelau', 'TL': 'Timor-Leste',
    'TM': 'Turkmenistan', 'TN': 'Tunisia', 'TO': 'Tonga', 'TR': 'Turkey',
    'TT': 'Trinidad and Tobago', 'TV': 'Tuvalu', 'TW': 'Taiwan',
    'TZ': 'Tanzania', 'UA': 'Ukraine', 'UG': 'Uganda',
    'UM': 'United States Minor Outlying Islands', 'US': 'United States',
    'UY': 'Uruguay', 'UZ': 'Uzbekistan', 'VA': 'Vatican City',
    'VC': 'Saint Vincent and the Grenadines', 'VE': 'Venezuela',
    'VG': 'British Virgin Islands', 'VI': 'U.S. Virgin Islands',
    'VN': 'Vietnam', 'VU': 'Vanuatu', 'WF': 'Wallis and Futuna', 'WS': 'Samoa',
    'XK': 'Kosovo', 'YE': 'Yemen', 'YT': 'Mayotte', 'ZA': 'South Africa',
    'ZM': 'Zambia', 'ZW': 'Zimbabwe',
}

# Other ways a location field writes a country: English variants, the
# country's own name, German names (German boards use them), and the parts
# of the United Kingdom. Keys are written without accents, in lower case.
ALIASES = {
    # United Kingdom and United States
    'uk': 'GB', 'u.k.': 'GB', 'great britain': 'GB', 'britain': 'GB',
    'england': 'GB', 'scotland': 'GB', 'wales': 'GB',
    'northern ireland': 'GB', 'vereinigtes konigreich': 'GB',
    'grossbritannien': 'GB',
    'usa': 'US', 'u.s.a.': 'US', 'u.s.a': 'US', 'u.s.': 'US',
    'united states of america': 'US', 'vereinigte staaten': 'US',
    # Own-language and alternative names
    'deutschland': 'DE', 'osterreich': 'AT', 'oesterreich': 'AT',
    'schweiz': 'CH', 'suisse': 'CH', 'svizzera': 'CH', 'srbija': 'RS',
    'serbien': 'RS', 'bosna i hercegovina': 'BA', 'bosnia & herzegovina': 'BA',
    'bosnia-herzegovina': 'BA', 'bosnia': 'BA', 'bih': 'BA',
    'czech republic': 'CZ', 'ceska republika': 'CZ', 'tschechien': 'CZ',
    'holland': 'NL', 'nederland': 'NL', 'the netherlands': 'NL',
    'niederlande': 'NL', 'turkiye': 'TR', 'tuerkiye': 'TR', 'turkei': 'TR',
    'korea': 'KR', 'republic of korea': 'KR', 'viet nam': 'VN',
    'uae': 'AE', 'dubai': 'AE', 'abu dhabi': 'AE',
    'russian federation': 'RU', 'macedonia': 'MK', 'hrvatska': 'HR',
    'kroatien': 'HR', 'crna gora': 'ME', 'espana': 'ES', 'spanien': 'ES',
    'italia': 'IT', 'italien': 'IT', 'polska': 'PL', 'polen': 'PL',
    'danmark': 'DK', 'danemark': 'DK', 'sverige': 'SE', 'schweden': 'SE',
    'norge': 'NO', 'norwegen': 'NO', 'suomi': 'FI', 'finnland': 'FI',
    'belgique': 'BE', 'belgie': 'BE', 'belgien': 'BE', 'luxemburg': 'LU',
    'frankreich': 'FR', 'ungarn': 'HU', 'rumanien': 'RO', 'bulgarien': 'BG',
    'griechenland': 'GR', 'slowakei': 'SK', 'slovak republic': 'SK',
    'slowenien': 'SI', 'slovenija': 'SI', 'irland': 'IE',
    'republic of ireland': 'IE', 'brasil': 'BR', 'brasilien': 'BR',
    'mexiko': 'MX', 'kanada': 'CA', 'indien': 'IN', 'ivory coast': 'CI',
    'cote divoire': 'CI', 'cape verde': 'CV', 'swaziland': 'SZ',
    'burma': 'MM', 'east timor': 'TL', 'macau': 'MO',
    # Sub-national names that settle the country on their own
    'new south wales': 'AU', 'queensland': 'AU', 'tasmania': 'AU',
    'ontario': 'CA', 'quebec': 'CA', 'british columbia': 'CA',
    'alberta': 'CA', 'manitoba': 'CA', 'saskatchewan': 'CA',
    'nova scotia': 'CA', 'new brunswick': 'CA', 'newfoundland': 'CA',
}

US_STATE_NAMES = (
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado',
    'connecticut', 'delaware', 'florida', 'hawaii', 'idaho', 'illinois',
    'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine', 'maryland',
    'massachusetts', 'michigan', 'minnesota', 'mississippi', 'missouri',
    'montana', 'nebraska', 'nevada', 'new hampshire', 'new jersey',
    'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio',
    'oklahoma', 'oregon', 'pennsylvania', 'rhode island', 'south carolina',
    'south dakota', 'tennessee', 'texas', 'utah', 'vermont', 'virginia',
    'washington', 'west virginia', 'wisconsin', 'wyoming',
    'district of columbia',
)

US_STATE_CODES = frozenset((
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
))

EU_CODES = frozenset((
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR',
    'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK',
    'SI', 'ES', 'SE',
))
EEA_CODES = EU_CODES | {'IS', 'LI', 'NO'}

# Region words. Groups of named countries resolve to those countries.
REGION_WORDS = {
    'eu': {'EU'}, 'european union': {'EU'}, 'eea': {'EU'},
    'european economic area': {'EU'}, 'schengen': {'EU'},
    'europe': {'EUROPE'}, 'europa': {'EUROPE'}, 'emea': {'EUROPE'},
    'balkans': {'EUROPE'},
    'dach': {'DE', 'AT', 'CH'}, 'benelux': {'BE', 'NL', 'LU'},
    'nordics': {'DK', 'SE', 'NO', 'FI', 'IS'}, 'nordic': {'DK', 'SE', 'NO', 'FI', 'IS'},
    'baltics': {'EE', 'LV', 'LT'},
    'apac': {'OTHER_REGION'}, 'asia pacific': {'OTHER_REGION'},
    'asia-pacific': {'OTHER_REGION'}, 'asia': {'OTHER_REGION'},
    'latam': {'OTHER_REGION'}, 'latin america': {'OTHER_REGION'},
    'amer': {'OTHER_REGION'}, 'amers': {'OTHER_REGION'},
    'north america': {'OTHER_REGION'}, 'south america': {'OTHER_REGION'},
    'central america': {'OTHER_REGION'}, 'americas': {'OTHER_REGION'},
    'middle east': {'OTHER_REGION'}, 'mena': {'OTHER_REGION'},
    'gcc': {'OTHER_REGION'}, 'africa': {'OTHER_REGION'},
    'oceania': {'OTHER_REGION'}, 'anz': {'OTHER_REGION'},
    'caribbean': {'OTHER_REGION'},
    'worldwide': {'WORLDWIDE'}, 'world wide': {'WORLDWIDE'},
    'global': {'WORLDWIDE'}, 'globally': {'WORLDWIDE'},
    'anywhere': {'WORLDWIDE'},
}

# A name with two readings. Both outside most allow-lists, so returning both
# lets the caller decide (it drops either way unless one reading is allowed).
AMBIGUOUS = {'georgia': {'GE', 'US'}}

# Large job hubs that boards write without a country: "San Francisco - SF9",
# "King Abdullah Economic City, 02 (02)". Only names with one well-known
# reading are listed; a name shared with a town elsewhere (Lagos, Perth,
# Melbourne, Vancouver, Hyderabad) is left out. Unlike ALIASES, a city is not
# one of the country's names (names_for), so "authorized to work in Boston"
# is not read as a US work-authorization demand.
CITY_COUNTRIES = {
    'san francisco': 'US', 'los angeles': 'US', 'san diego': 'US',
    'chicago': 'US', 'seattle': 'US', 'boston': 'US', 'denver': 'US',
    'atlanta': 'US', 'houston': 'US', 'dallas': 'US', 'philadelphia': 'US',
    'pittsburgh': 'US', 'detroit': 'US', 'miami': 'US', 'palo alto': 'US',
    'mountain view': 'US', 'menlo park': 'US', 'sunnyvale': 'US',
    'cupertino': 'US', 'redmond': 'US',
    'toronto': 'CA', 'montreal': 'CA', 'ottawa': 'CA', 'calgary': 'CA',
    'riyadh': 'SA', 'jeddah': 'SA', 'dammam': 'SA', 'dhahran': 'SA',
    'al khobar': 'SA', 'jubail': 'SA', 'yanbu': 'SA', 'neom': 'SA',
    'king abdullah economic city': 'SA', 'kaec': 'SA',
    'doha': 'QA', 'tel aviv': 'IL', 'istanbul': 'TR', 'ankara': 'TR',
    'bangalore': 'IN', 'bengaluru': 'IN', 'mumbai': 'IN', 'chennai': 'IN',
    'gurgaon': 'IN', 'gurugram': 'IN', 'noida': 'IN', 'new delhi': 'IN',
    'shanghai': 'CN', 'beijing': 'CN', 'shenzhen': 'CN', 'tokyo': 'JP',
    'osaka': 'JP', 'seoul': 'KR', 'kuala lumpur': 'MY', 'manila': 'PH',
    'bangkok': 'TH', 'jakarta': 'ID', 'sydney': 'AU', 'brisbane': 'AU',
    'sao paulo': 'BR', 'bogota': 'CO', 'buenos aires': 'AR',
    'johannesburg': 'ZA', 'cape town': 'ZA', 'nairobi': 'KE', 'cairo': 'EG',
}


def _build_lookup():
    lookup = {}
    for code, name in COUNTRY_NAMES.items():
        lookup.setdefault(fold_diacritics(name), set()).add(code)
    for name, code in ALIASES.items():
        lookup.setdefault(name, set()).add(code)
    for name in US_STATE_NAMES:
        lookup.setdefault(name, set()).add('US')
    for name, codes in REGION_WORDS.items():
        lookup.setdefault(name, set()).update(codes)
    for name, codes in AMBIGUOUS.items():
        lookup[name] = set(codes)
    return lookup


_LOOKUP = _build_lookup()


def _names_pattern(names):
    return re.compile(
        r'(?<![a-z0-9])('
        + '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        + r')(?![a-z0-9])')


_NAME_RE = _names_pattern(_LOOKUP)
_CITY_RE = _names_pattern(CITY_COUNTRIES)
# "US" in capitals, as in "Remote - US" or "US-Remote". Lower-case "us" is an
# ordinary word, so it is never read as a country.
_US_TOKEN_RE = re.compile(r'(?<![A-Za-z])US(?![A-Za-z])')
_SEGMENT_SPLIT = re.compile(r'[|;/\n]| or ')


def iso_name(code):
    """English short name for an ISO code, or '' when the code is unknown."""
    return COUNTRY_NAMES.get((code or '').upper(), '')


def _named_codes(text, cities=True):
    """The codes and tokens named in text by a country, state or region name,
    and by a city in CITY_COUNTRIES unless cities is False."""
    folded = re.sub(r'\s+', ' ', fold_diacritics(text or '').replace('’', "'"))
    codes = set()
    for match in _NAME_RE.finditer(folded):
        codes |= _LOOKUP[match.group(1)]
    if cities:
        codes |= {CITY_COUNTRIES[m.group(1)] for m in _CITY_RE.finditer(folded)}
    if _US_TOKEN_RE.search(text or ''):
        codes.add('US')
    return codes


def _names_another_country(text):
    """True when text names a country other than the United States, by name
    or as a bare upper-case ISO code ("NL" in "Almere, FL, NL")."""
    text = (text or '').strip()
    if re.fullmatch(r'[A-Z]{2}', text) and text != 'US' and text in COUNTRY_NAMES:
        return True
    return any(c in COUNTRY_NAMES and c != 'US' for c in _named_codes(text))


def _strip_postcode(parts):
    """
    The parts without the trailing ones that hold a postcode, and those
    dropped parts. A part that starts with one ("1327 AE", "411013", "39014
    (BZ)") is dropped. So is a state followed by a postcode ("VIC 3170", "MN
    55447") when an upper-case ISO country code sits before it, the shape
    "Mulgrave, AU, VIC 3170". "Austin, TX 78701" has no country code before
    the state, so "TX 78701" is kept and read as Texas.
    """
    core = list(parts)
    while len(core) > 1 and re.match(r'\S*\d', core[-1]):
        core.pop()
    if (len(core) >= 3 and re.fullmatch(r'[A-Z]{2,3}\s+\d{3,5}', core[-1])
            and core[-2] in COUNTRY_NAMES and core[-2] not in US_STATE_CODES):
        core.pop()
    return core, parts[len(core):]


def _reads_as_state(core, i, dropped):
    """
    Whether the two-letter code starting core[i] is a US state.

    Only when nothing after it says otherwise. "Austin, TX" and "Austin, TX
    78701" are Texas. "Almere, FL, NL" is Flevoland in the Netherlands,
    "Truccazzano, MI, IT" is the province of Milan, and "Pune, IN, 411013"
    ends in a six-digit postcode, which no US address has.
    """
    rest = ' '.join(core[i].split()[1:])
    if _names_another_country(rest):
        return False
    if i < len(core) - 1:
        return not _names_another_country(core[i + 1])
    if dropped:
        return bool(re.fullmatch(r'\d{5}(?:-\d{4})?', dropped[0]))
    return True


def location_countries(location, protect=frozenset(), cities=True):
    """
    The country codes and region tokens a location field names.

    protect: two-letter codes that must not be read as US state codes after
    a comma, because they are also the code of an allowed country.
    cities: False ignores CITY_COUNTRIES, for a caller asking whether the
    field already spells out its country.

    A two-letter code after a comma is read three ways:
      - as the ISO country, when it is upper case, ends the location and
        follows a region or comes before a postcode, the SuccessFactors shape
        ("Munich, BY, DE", "Pune, IN, 411013")
      - as a US state, when it is a state code and nothing after it names
        another country ("Austin, TX"; not "Almere, FL, NL")
      - as the ISO country, when it is lower case and ends the location, the
        form SmartRecruiters used to produce ("Stuttgart, de")
    A bare "City, ST" stays ambiguous: "Milano, MI" reads as Michigan, since
    telling the two apart would need a list of cities.
    """
    text = location or ''
    if not text.strip():
        return set()
    codes = _named_codes(text, cities=cities)

    for segment in _SEGMENT_SPLIT.split(text):
        parts = [p.strip() for p in segment.split(',')]
        core, dropped = _strip_postcode(parts)
        last = core[-1] if len(core) > 1 else ''
        iso_last = (bool(re.fullmatch(r'[A-Z]{2}', last)) and last in COUNTRY_NAMES
                    and (bool(dropped) or len(core) >= 3))
        if iso_last:
            codes.add(last)
        for i in range(1, len(core) - (1 if iso_last else 0)):
            words = core[i].split()
            token = words[0] if words else ''
            if (token in US_STATE_CODES and token not in protect
                    and _reads_as_state(core, i, dropped)):
                codes.add('US')
        if re.fullmatch(r'[a-z]{2}', last) and last.upper() in COUNTRY_NAMES:
            codes.add(last.upper())
    return codes


def expand_codes(tokens):
    """
    Turn a user's list (ISO codes plus EU, EEA and EUROPE) into the set of
    codes and tokens it covers. EU and EEA cover their member states and EU-
    wide wording. EUROPE covers Europe-wide wording only, not each country.
    """
    out = set()
    for token in tokens or ():
        token = (token or '').strip().upper()
        if not token:
            continue
        if token == 'EU':
            out |= EU_CODES | {'EU'}
        elif token == 'EEA':
            out |= EEA_CODES | {'EU'}
        elif token == 'EUROPE':
            out |= {'EUROPE', 'EU'}
        elif token == 'UK':
            out.add('GB')
        else:
            out.add(token)
    return out


def names_for(code):
    """Every lower-case, accent-free name this module reads as `code`."""
    code = (code or '').upper()
    names = {fold_diacritics(COUNTRY_NAMES[code])} if code in COUNTRY_NAMES else set()
    names |= {name for name, c in ALIASES.items() if c == code}
    if code == 'US':
        names.add('us')
    return names


def spell_out_trailing_code(location):
    """
    Replace an ISO country code at the end of a location with its name:
    "Vaasa, FI" becomes "Vaasa, Finland" and "Heidenheim, BW (DE)" becomes
    "Heidenheim, BW (Germany)". Only for sources known to write ISO codes
    there; elsewhere a trailing "CA" is far more often California.
    """
    text = location or ''

    def replace(match):
        name = iso_name(match.group(2))
        return f"{match.group(1)}{name}{match.group(3)}" if name else match.group(0)

    return re.sub(r'(,\s*|\()([A-Z]{2})(\)?)\s*$', replace, text)


def successfactors_location(location):
    """
    A SuccessFactors location with its postcode removed and its ISO country
    code spelled out.

    The feed writes "City, REGION, CC, postcode": "Almere, FL, NL, 1327 AE"
    becomes "Almere, FL, Netherlands", "Pune, IN, 411013" becomes "Pune,
    India", and "Burgstall, IT, 39014 (BZ)" becomes "Burgstall, Italy". The
    old code looked only at the very end, so it found the postcode, or read
    the province "(BZ)" as Belize. Forms without a postcode ("Vaasa, FI",
    "Heidenheim, BW (DE)") are spelled out as before.
    """
    text = (location or '').strip()
    if not text:
        return text
    parts, _ = _strip_postcode([p.strip() for p in text.split(',')])
    last = parts[-1]
    if len(parts) > 1 and re.fullmatch(r'[A-Z]{2}', last) and last in COUNTRY_NAMES:
        parts[-1] = COUNTRY_NAMES[last]
        return ', '.join(parts)
    return spell_out_trailing_code(', '.join(parts))


_REMOTE_PLACE = re.compile(
    r'\b(?:remote|anywhere|worldwide|global|distributed|virtual|home[- ]based|'
    r'work from home)\b')


def is_remote_place(location):
    """True when a location field says the job is remote or location-free."""
    return bool(_REMOTE_PLACE.search(fold_diacritics(location or '')))


# Template words a board leaves in place of a real location: Danfoss writes
# "City-State-Country, City-State-Country, City-State-Country", Workday "2
# Locations". Only these words, digits and punctuation, nothing else.
_PLACEHOLDER_WORDS = re.compile(
    r'(?:[\s,;/|()\-]|\d|city|state|province|region|country|countries|'
    r'locations?|multiple|various|tbd|tba)+')


def is_placeholder_location(location):
    """True for a location made only of template words ("City-State-Country",
    "2 Locations", "Multiple locations"), which names no place at all. An
    empty field, "Remote" or a bare city name is not a placeholder."""
    folded = fold_diacritics(location or '').strip()
    return bool(re.search(r'[a-z]', folded) and _PLACEHOLDER_WORDS.fullmatch(folded))


def text_countries(text):
    """The country codes and region tokens a piece of advert text names, read
    the same way as a location field's names. Cities count, see
    CITY_COUNTRIES. Used only when the location is a placeholder."""
    return _named_codes(text)


# A remote job tied to one country: "United States – Remote", "Remote -
# Germany", "Remote (UK)" in the advert, or "...-united-states-remote" in the
# link. Google Jobs labels every remote job "Anywhere", which reads as
# worldwide, so a US-only remote role reached a Europe-only digest.
_REMOTE_BOUND = re.compile(
    r'([a-z][a-z .]{1,40}?)\s*[-–—]\s*remote\b'
    r'|\bremote\s*[-–—(:]\s*([a-z][a-z .]{1,40})')


def remote_countries(text, link=''):
    """
    The countries a remote job says it is remote in, from the advert text
    and the link's path, as country codes. Empty when it names none.
    """
    found = set()
    windows = [fold_diacritics(text or '')]
    path = urlparse(link or '').path if link else ''
    if path:
        words = re.split(r'[/_\-.]+', fold_diacritics(path))
        for i, word in enumerate(words):
            if word == 'remote':
                windows.append(' '.join(words[max(0, i - 3):i]))
                windows.append(' '.join(words[i + 1:i + 4]))
    for window in windows[1:]:
        found |= _named_codes(window)
    for match in _REMOTE_BOUND.finditer(windows[0]):
        found |= _named_codes(match.group(1) or match.group(2))
    return {code for code in found if code in COUNTRY_NAMES}


def with_countries(location, countries):
    """
    Append country names a location lacks, in brackets: "San Francisco"
    with ["United States"] becomes "San Francisco (United States)". A country
    the location already names is not repeated. Empty names are skipped.
    """
    location = location or ''
    present = location_countries(location, cities=False)
    missing = []
    for name in countries:
        name = (name or '').strip()
        if not name or name in missing:
            continue
        resolved = location_countries(name)
        if resolved and resolved <= present:
            continue
        missing.append(name)
        present |= resolved
    if not missing:
        return location
    if not location.strip() or location.strip().lower() == 'not stated':
        return ', '.join(missing)
    return f"{location} ({', '.join(missing)})"
