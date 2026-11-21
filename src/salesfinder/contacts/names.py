"""Person names: normalization, matching keys, first/last split, and a strict 'is this a person
name?' gate. Pure functions; never raise on odd input."""

import re
import unicodedata

_WS = re.compile(r"\s+")
_HONORIFIC = re.compile(r"(?:mrs|mr|ms|miss|dr|shri|smt|sri|prof|er)(?:\.\s*|\s+)", re.IGNORECASE)
_EDGE_JUNK = ' \t,;:|/\\-\u2013\u2014\u2022\u00b7*"\u201c\u201d'
_INITIAL = re.compile(r"(?:[^\W\d_]\.)+[^\W\d_]?|[^\W\d_]", re.UNICODE)
_KEY_DROP = re.compile(r"[^a-z ]+")

# Lowercase name particles allowed between capitalized tokens ("Ludwig van Beethoven").
_PARTICLES = frozenset(
    {"de", "da", "das", "dos", "del", "della", "di", "du", "van", "von", "der", "den", "la", "le"}
    | {"bin", "binti", "al", "el", "ter", "ten"}
)

# Words that never appear in a person's name on a company page: company-name parts, navigation,
# departments, job-title words, places-of-business and product/chemistry words. A capitalized
# two-word phrase containing one of these is not a person ("Contact Us", "Purchase Dept",
# "Managing Director", "Head Office", "Sodium Benzoate").
# fmt: off
STOP_WORDS: tuple[str, ...] = (
    # company forms and kinds
    "ltd", "limited", "pvt", "private", "inc", "incorporated", "llc", "llp", "lp", "plc", "gmbh",
    "corp", "corporation", "company", "co", "industries", "industry", "industrial", "chemicals",
    "chemical", "group", "holdings", "enterprises", "enterprise", "traders", "trading", "agencies",
    "agency", "solutions", "technologies", "technology", "tech", "systems", "pharma",
    "pharmaceuticals", "polymers", "plastics", "organics", "labs", "laboratories", "laboratory",
    "impex", "overseas", "international", "global", "worldwide", "exim", "brothers", "bros",
    "associates", "consultants", "products", "product", "services", "service", "manufacturers",
    "manufacturer", "manufacturing", "suppliers", "supplier", "distributors", "distributor",
    "dealers", "exporters", "exporter", "importers", "specialty", "speciality", "fine",
    # more organisation forms and kinds ("Brenntag AG", "Tata Capital", "Asian Paints")
    "ag", "sa", "bv", "nv", "srl", "spa", "sarl", "pte", "pty", "bhd", "sdn", "ltda", "kk",
    "partners", "ventures", "capital", "bank", "banks", "university", "institute", "foundation",
    "consulting", "consultancy", "biotech", "healthcare", "lifesciences", "foods", "food",
    "agrochemicals", "flavours", "flavors", "fragrances", "cosmetics", "nutrition",
    "intermediates", "additives", "lubricants", "formulations", "textiles", "packaging",
    "rubber", "petroleum", "refinery", "energy",
    # navigation and page furniture
    "home", "about", "contact", "team", "our", "us", "we", "you", "your", "read", "more", "click",
    "here", "view", "learn", "know", "see", "find", "get", "go", "back", "top", "next", "previous",
    "prev", "menu", "search", "submit", "send", "login", "logout", "sign", "register", "account",
    "cart", "shop", "store", "buy", "order", "quote", "request", "enquiry", "enquire", "inquiry",
    "call", "email", "mail", "phone", "mobile", "fax", "tel", "address", "map", "maps", "location",
    "locations", "directions", "news", "blog", "events", "event", "gallery", "media", "videos",
    "downloads", "download", "brochure", "catalogue", "catalog", "careers", "career", "jobs",
    "privacy", "policy", "terms", "conditions", "cookie", "cookies", "disclaimer", "sitemap",
    "copyright", "rights", "reserved", "all", "welcome", "follow", "share", "like", "facebook",
    "twitter", "linkedin", "instagram", "youtube", "whatsapp", "quick", "links", "useful",
    "overview", "vision", "mission", "values", "history", "profile", "message", "infrastructure",
    "certifications", "certificate", "awards", "clients", "testimonials", "faq", "faqs", "help",
    "meet", "leadership", "management", "board", "directors", "people", "staff", "members",
    "the", "and", "of", "for", "with", "to", "in", "at", "by", "on", "new", "now", "today",
    "name", "first", "last", "full", "designation", "subject", "comments", "required",
    # departments and functions
    "department", "dept", "division", "office", "head", "registered", "corporate", "branch",
    "factory", "plant", "works", "unit", "warehouse", "sales", "support", "quality", "purchase",
    "purchasing", "procurement", "export", "exports", "import", "imports", "marketing",
    "business", "development", "logistics", "supply", "chain", "research", "technical",
    "operations", "production", "finance", "accounts", "admin", "administration", "human",
    "resources", "hr", "legal", "customer", "care", "helpdesk", "desk", "center", "centre",
    # job-title words
    "manager", "managing", "director", "executive", "officer", "chief", "president", "vice",
    "founder", "cofounder", "owner", "proprietor", "partner", "chairman", "chairperson", "ceo",
    "cfo", "coo", "cto", "md", "gm", "vp", "senior", "sr", "junior", "jr", "assistant",
    "associate", "general", "deputy", "engineer", "chemist", "scientist", "accountant",
    "consultant", "advisor", "secretary", "coordinator", "supervisor", "specialist", "analyst",
    "lead", "trainee", "intern",
    # chemistry / product words
    "acid", "oxide", "dioxide", "sodium", "potassium", "calcium", "magnesium", "ammonium",
    "chloride", "sulphate", "sulfate", "carbonate", "bicarbonate", "nitrate", "phosphate",
    "acetate", "citrate", "benzoate", "stearate", "silicate", "hydroxide", "peroxide", "glycol",
    "resin", "resins", "oil", "oils", "wax", "powder", "solution", "grade", "polymer", "ester",
    "esters", "alcohol", "solvent", "solvents", "pigment", "pigments", "dye", "dyes", "zinc",
    "iron", "ferric", "ferrous", "copper", "titanium", "aluminium", "aluminum", "sulphur",
    "sulfur", "carbon", "ethyl", "methyl", "propyl", "butyl", "ethanol", "methanol", "acetone",
    "toluene", "xylene", "benzene", "water", "salt", "salts", "gum", "extract", "extracts",
    "alkali", "alkalies", "alkalis", "fertilizers", "fertilisers", "paints", "coatings", "dyestuff",
    "dyestuffs", "minerals", "metals", "petrochemicals", "agro", "chemistry",
    # qualifications ("PhD Chemistry", "B.Tech, MBA")
    "phd", "mba", "msc", "bsc", "btech", "mtech", "bcom", "mcom", "bpharm", "mpharm", "ca", "cs",
    "cma", "diploma", "graduate", "engineering", "science", "sciences", "doctorate",
    # template placeholders ("John Doe", "Lorem Ipsum")
    "doe", "lorem", "ipsum", "dolor", "sample", "placeholder",
    # places and regions (the commonest in "City - Office" / "Region - Sales Head" lines)
    "india", "indian", "north", "south", "east", "west", "central", "region", "zone", "navi",
    "mumbai", "bombay", "delhi", "bangalore", "bengaluru", "chennai", "madras", "kolkata",
    "calcutta", "hyderabad", "pune", "ahmedabad", "surat", "vadodara", "baroda", "jaipur",
    "lucknow", "kanpur", "nagpur", "indore", "thane", "bhopal", "ludhiana", "nashik", "rajkot",
    "coimbatore", "kochi", "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "chandigarh",
    "ankleshwar", "vapi", "bharuch", "dahej", "silvassa", "gujarat", "maharashtra", "karnataka",
    "telangana", "kerala", "punjab", "haryana", "rajasthan", "pradesh", "bengal", "odisha",
    "andheri", "usa", "america", "american", "texas", "california", "york", "jersey", "houston",
    "chicago", "europe", "asia", "africa", "middle",
    # time words
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january",
    "february", "july", "september", "october", "november", "december", "open", "closed",
)
# fmt: on
_STOP = frozenset(STOP_WORDS)


def normalize_name(raw: str) -> str:
    """Honorifics stripped, whitespace collapsed, and title-cased only when written ALL CAPS."""
    text = _WS.sub(" ", unicodedata.normalize("NFC", raw)).strip(_EDGE_JUNK)
    while (m := _HONORIFIC.match(text)) is not None:
        text = text[m.end() :].strip(_EDGE_JUNK)
    letters = [c for c in text if c.isalpha()]
    if len(letters) > 1 and all(c.isupper() for c in letters):
        text = text.title()
    return text


def name_key(name: str) -> str:
    """Lowercase ASCII letters-and-spaces key of a name, for deduplication and matching."""
    ascii_text = (
        unicodedata.normalize("NFKD", normalize_name(name))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return " ".join(_KEY_DROP.sub("", w) for w in ascii_text.split() if _KEY_DROP.sub("", w))


def _is_initial(token: str) -> bool:
    return _INITIAL.fullmatch(token) is not None and token[0].isupper()


def split_name(name: str) -> tuple[str | None, str | None]:
    """First and last name tokens; an initial ("R.", "K") is never used as either."""
    tokens = normalize_name(name).split()
    if not tokens:
        return None, None
    first = None if _is_initial(tokens[0]) else tokens[0]
    last = None if len(tokens) < 2 or _is_initial(tokens[-1]) else tokens[-1]
    return first, last


def _is_word(token: str) -> bool:
    """A capitalized name word: letters plus apostrophes/hyphens, at least two letters."""
    if not token[0].isupper() or not token[0].isalpha():
        return False
    if not all(c.isalpha() or c in "'\u2019-" for c in token):
        return False
    return sum(c.isalpha() for c in token) >= 2


def is_stop_word(token: str) -> bool:
    """Whether ``token`` (or a hyphenated part of it) is a word that is never part of a person's
    name ("Ltd", "Sales", "Mumbai", "Ph.D.")."""
    bare = token.lower().strip(".'\u2019")
    # The dot-stripped lookup catches "Ph.D." / "M.B.A."; it skips short dotted initials
    # ("K.K.", "S.R.", "V.P.", "M.D.", "A.G."), which are people's initials, not "kk"/"sr"/"vp".
    dotted = bare.replace(".", "")
    short_initial = _is_initial(token) and len(dotted) <= 2
    return (
        bare in _STOP
        or (not short_initial and dotted in _STOP)
        or any(part in _STOP for part in bare.split("-") if part)
    )


_is_stop = is_stop_word


def looks_like_person_name(text: str) -> bool:
    """2-4 capitalized tokens or initials, at most 40 characters, no digits and no stop word."""
    if not text or len(text) > 120 or any(c.isdigit() for c in text):
        return False
    name = normalize_name(text)
    tokens = name.split()
    if not 2 <= len(tokens) <= 4 or len(name) > 40:
        return False
    words = 0
    for i, token in enumerate(tokens):
        if _is_stop(token):
            return False
        if _is_word(token):
            words += 1
        elif not _is_initial(token) and not (0 < i < len(tokens) - 1 and token in _PARTICLES):
            return False
    return words >= 1
