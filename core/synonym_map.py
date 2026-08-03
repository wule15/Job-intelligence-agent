"""
Synonym / alias map for job matching.
Maps CV skill terms → job-posting equivalents.

How it works: when checking whether skill X appears in a job description,
we also check if any synonym for X appears. This bridges the vocabulary gap
between how CVs are written and how job ads are written.

Add new entries freely — the more the better.
"""

# Each key is a canonical skill term (lowercase).
# Each value is a list of additional terms that mean the same thing
# and are likely to appear in job postings.
SYNONYMS: dict[str, list[str]] = {

    # ── Mechanical / Fluid / Aero ────────────────────────────────────────────
    "cfd": [
        "computational fluid dynamics", "fluid simulation", "flow simulation",
        "fluid dynamics", "cfd analysis", "cfd modelling", "cfd modeling",
    ],
    "cfd simulation": [
        "computational fluid dynamics", "fluid simulation", "cfd analysis",
        "flow analysis", "cfd modelling",
    ],
    "solidworks": [
        "solid works", "solidworks cad", "3d cad", "3d modeling", "cad design",
        "parametric cad",
    ],
    "solidworks flow simulation": [
        "flow simulation", "solidworks simulation", "cfd",
    ],
    "autocad": [
        "auto cad", "cad drafting", "2d cad", "cad design", "drafting",
    ],
    "inventor": [
        "autodesk inventor", "3d cad", "parametric modeling",
    ],
    "star-ccm+": [
        "star ccm", "siemens star", "cfd software", "cfd",
        "computational fluid dynamics",
    ],
    "openfoam": [
        "open foam", "open-source cfd", "cfd", "computational fluid dynamics",
    ],
    "ansys": [
        "ansys fluent", "ansys mechanical", "fea", "finite element analysis",
        "simulation software", "cfd",
    ],
    "fluid systems": [
        "hydraulics", "hydraulic systems", "fluid power", "flow control",
        "process fluids", "piping systems",
    ],
    "process engineering": [
        "process design", "process optimization", "process improvement",
        "industrial process", "chemical process", "process development",
        "process engineer",
    ],
    "mechanical engineering": [
        "mechanical design", "mechanical systems", "mechanical engineer",
        "product design", "engineering design", "machine design",
    ],
    "aerodynamics": [
        "aerodynamic analysis", "aero engineering", "wind tunnel",
        "drag reduction", "lift analysis", "f1 aerodynamics",
    ],
    "p&id review": [
        "piping and instrumentation", "p&id", "process diagrams",
        "instrumentation diagrams", "plant design",
    ],
    "component sizing": [
        "equipment sizing", "mechanical sizing", "component selection",
        "valve sizing", "pump sizing",
    ],
    "flow simulation": [
        "cfd", "fluid simulation", "flow analysis", "computational fluid dynamics",
    ],

    # ── Valves / O&G ─────────────────────────────────────────────────────────
    "valve": [
        "valves", "control valve", "flow control", "valve engineering",
        "valve selection", "valve sizing", "ball valve", "gate valve",
        "check valve", "pressure control",
    ],
    "csam valve management": [
        "valve management", "asset management", "valve lifecycle",
        "industrial valve", "valve database",
    ],
    "sam": [
        "valve management software", "asset management software",
    ],
    "oil and gas": [
        "oil & gas", "o&g", "upstream", "downstream", "midstream",
        "petroleum", "refinery", "energy sector", "oilfield",
    ],
    "procurement": [
        "purchasing", "sourcing", "supply chain", "vendor management",
        "strategic sourcing", "technical procurement", "category management",
    ],

    # ── Sales / Business Development ─────────────────────────────────────────
    "sales engineering": [
        "sales engineer", "technical sales", "pre-sales", "presales",
        "solutions engineer", "application engineer", "technical account manager",
        "technical sales manager", "inside sales engineer",
    ],
    "b2b": [
        "business to business", "b2b sales", "enterprise sales",
        "b2b marketing", "industrial sales", "commercial sales",
    ],
    "b2b content strategy": [
        "b2b content", "enterprise content", "business content strategy",
        "content marketing", "b2b marketing",
    ],
    "technical sales": [
        "sales engineer", "pre-sales", "presales", "solutions engineer",
        "application engineer", "technical account manager",
    ],
    "account management": [
        "account manager", "client management", "key account", "customer success",
        "client relations", "account executive",
    ],
    "crm": [
        "salesforce", "hubspot", "customer relationship management",
        "crm software", "sales crm",
    ],

    # ── Project / Operations Management ──────────────────────────────────────
    "project management": [
        "program management", "project delivery", "project coordination",
        "project lead", "project manager", "pmo", "project planning",
        "delivery management", "project execution",
    ],
    "technical project management": [
        "technical project manager", "engineering project management",
        "project management", "program management",
    ],
    "agile": [
        "scrum", "kanban", "sprint", "agile methodology", "agile project management",
        "agile delivery",
    ],

    # ── Content / Writing / SEO ───────────────────────────────────────────────
    "content strategy": [
        "content marketing", "content management", "content planning",
        "editorial strategy", "content development", "content lead",
        "content strategist",
    ],
    "technical documentation": [
        "technical writing", "documentation", "technical writer",
        "docs", "user documentation", "api documentation",
        "knowledge base", "product documentation",
    ],
    "technical writing": [
        "technical documentation", "technical writer", "documentation",
        "user guides", "manuals",
    ],
    "seo": [
        "search engine optimization", "search optimization", "organic search",
        "on-page seo", "off-page seo", "technical seo", "seo strategy",
    ],
    "technical seo": [
        "seo", "search engine optimization", "site audit", "technical optimization",
    ],
    "seo auditing": [
        "seo audit", "site audit", "technical seo", "seo analysis",
    ],
    "content architecture": [
        "information architecture", "content structure", "content design",
        "content hierarchy",
    ],
    "analytics (ga4)": [
        "google analytics", "ga4", "web analytics", "analytics", "data analytics",
    ],
    "google search console": [
        "search console", "gsc", "google webmaster tools",
    ],
    "semrush": [
        "sem rush", "seo tools", "ahrefs", "moz", "seo platform",
    ],
    "ai-assisted workflows": [
        "ai tools", "ai workflow", "llm", "generative ai", "gpt", "ai automation",
        "ai integration", "chatgpt", "copilot",
    ],

    # ── AI / Automation ───────────────────────────────────────────────────────
    "artificial intelligence": [
        "ai", "machine learning", "ml", "deep learning", "llm",
        "large language model", "generative ai", "ai engineer",
    ],
    "machine learning": [
        "ml", "ai", "deep learning", "nlp", "neural networks",
        "predictive modeling", "data science",
    ],
    "industrial systems": [
        "industrial automation", "industrial engineering", "manufacturing systems",
        "scada", "plc", "industrial iot", "industry 4.0",
    ],
    "automation": [
        "process automation", "workflow automation", "rpa", "robotic process automation",
        "industrial automation", "test automation",
    ],

    # ── General Engineering / Technical ──────────────────────────────────────
    "f1 regulatory analysis": [
        "regulatory analysis", "compliance", "technical regulations",
        "engineering regulations",
    ],
    "simulation": [
        "cfd", "fea", "finite element", "numerical simulation",
        "modelling", "modeling", "computational analysis",
    ],
    "python": [
        "python programming", "python scripting", "python developer",
    ],
    "sql": [
        "database", "mysql", "postgresql", "data querying", "structured query language",
    ],
}


def expand_skill(skill: str) -> list[str]:
    """
    Return all terms to search for a given skill:
    the skill itself + all known synonyms (all lowercase).
    """
    base = skill.lower().strip()
    extras = SYNONYMS.get(base, [])
    return [base] + [s.lower() for s in extras]


def skill_matches(skill: str, text: str) -> bool:
    """
    Return True if the skill OR any of its synonyms appear in text.
    text should already be lowercased.
    """
    for term in expand_skill(skill):
        if term in text:
            return True
    return False
