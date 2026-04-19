"""dango/auth/security.py

Pure security utility functions for Dango authentication.

Provides password hashing (bcrypt via pwdlib), cryptographic token
generation, API key management, temporary passwords, and TOTP recovery
codes.  All functions are stateless and have no database or model
dependencies — they are consumed by higher-level auth modules
(sessions, CLI, Metabase sync).

Password policy follows NIST SP 800-63B: minimum length + common
password blocklist.  No complexity rules (digit/uppercase/special).
"""

from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_hasher = PasswordHash((BcryptHasher(rounds=12),))

_API_KEY_PREFIX = "dango_ak_"

_MIN_PASSWORD_LENGTH = 8

# Mixed case, no ambiguous characters (0, O, o, 1, l, I)
_TEMP_PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"

# Uppercase only, no ambiguous characters
_RECOVERY_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# ---------------------------------------------------------------------------
# Common passwords (top ~1000 from SecLists, all lowercase, O(1) lookup)
# ---------------------------------------------------------------------------

# fmt: off
_COMMON_PASSWORDS: frozenset[str] = frozenset({
    "0000", "000000", "007007", "101010", "1111", "11111", "111111", "11111111",
    "112233", "1212", "121212", "123123", "123321", "1234", "12345", "123456",
    "1234567", "12345678", "123456789", "123654", "123abc", "123qwe", "1313", "131313",
    "147147", "159753", "1969", "1q2w3e", "1q2w3e4r", "1qaz2wsx", "2000", "2112",
    "212121", "2222", "222222", "232323", "242424", "252525", "3333", "333333",
    "420420", "4321", "4444", "444444", "5150", "54321", "5555", "55555",
    "555555", "654321", "6666", "666666", "6969", "696969", "69696969", "7777",
    "777777", "7777777", "789456", "8675309", "87654321", "8888", "888888", "88888888",
    "987654", "9999", "999999", "aaaa", "aaaaaa", "aaaaaaaa", "abc123", "abcd",
    "abcd1234", "abcdef", "abcdefg", "access", "accord", "action", "adam", "adidas",
    "admin", "airborne", "airplane", "alaska", "albert", "alex", "alexande", "alexis",
    "alicia", "alison", "allison", "alpha", "alpha1", "alyssa", "amanda", "amateur",
    "amber", "america", "american", "anderson", "andrea", "andrew", "angel", "angela",
    "angels", "animal", "anthony", "apollo", "apple", "apples", "arizona", "arnold",
    "arsenal", "arthur", "asdf", "asdfasdf", "asdfg", "asdfgh", "asdfghjk", "ashley",
    "asshole", "assman", "august", "austin", "avalon", "azerty", "babes", "baby",
    "babygirl", "badass", "badboy", "badger", "bailey", "balls", "bambam", "banana",
    "bang", "barbara", "barney", "baseball", "bass", "bastard", "batman", "baxter",
    "bbbbbb", "beach", "bear", "beatles", "beaver", "beavis", "beer", "benjamin",
    "berlin", "bigboy", "bigcock", "bigdaddy", "bigdick", "bigdog", "bigmac", "bigman",
    "bigred", "bigtits", "bill", "billy", "birdie", "bishop", "bitch", "bitches",
    "biteme", "black", "blazer", "blink182", "blonde", "blowjob", "blowme", "blue",
    "bobby", "bollocks", "bond007", "bondage", "bonnie", "boobies", "booboo", "boobs",
    "booger", "boogie", "boomer", "booty", "boston", "bradley", "brandon", "brandy",
    "braves", "brazil", "brenda", "brian", "britney", "brittany", "bronco", "broncos",
    "brooke", "brooklyn", "brother", "brown", "brutus", "bubba", "bubba1", "bubbles",
    "buddha", "buddy", "budlight", "buffalo", "bulldog", "bulldogs", "bullet", "bullshit",
    "bunny", "burton", "buster", "butter", "butthead", "calvin", "camaro", "cameron",
    "canada", "candy", "cannon", "captain", "cardinal", "carlos", "carmen", "carolina",
    "caroline", "carrie", "carter", "cartman", "casper", "cassie", "catch22", "celtic",
    "champion", "chance", "charles", "charlie", "cheese", "chelsea", "cherokee", "cherry",
    "chester", "chevelle", "chevy", "chicago", "chicken", "chicks", "chopper", "chris",
    "chris1", "christ", "christin", "chronic", "claire", "classic", "claudia", "clinton",
    "cobra", "cocacola", "cock", "coffee", "college", "colorado", "compaq", "computer",
    "connie", "connor", "control", "cookie", "cool", "cooper", "copper", "corvette",
    "cougar", "courtney", "cowboy", "cowboys", "coyote", "crazy", "cream", "creative",
    "cricket", "crystal", "cumming", "cumshot", "cunt", "daddy", "dakota", "dallas",
    "dancer", "daniel", "danielle", "darkness", "dave", "david", "death", "debbie",
    "december", "denise", "dennis", "denver", "destiny", "devils", "dexter", "diablo",
    "diamond", "dick", "dickhead", "diesel", "digger", "digital", "dirty", "disney",
    "doctor", "dodgers", "doggie", "dolphin", "dolphins", "domino", "donald", "donkey",
    "douglas", "dragon", "dreamer", "dreams", "driver", "drowssap", "drummer", "ducati",
    "dude", "duke", "duncan", "eagle", "eagle1", "eagles", "eatme", "eclipse",
    "edward", "einstein", "electric", "elephant", "elizabet", "elvis", "eminem", "empire",
    "enigma", "enjoy", "enter", "eric", "erotic", "eugene", "everton", "explorer",
    "extreme", "falcon", "family", "fantasy", "fender", "ferrari", "fire", "fireman",
    "fish", "fisher", "fishing", "flash", "florida", "flower", "flowers", "fluffy",
    "flyers", "football", "ford", "forest", "forever", "france", "francis", "frank",
    "frankie", "franklin", "freaky", "fred", "freddy", "free", "freedom", "freepass",
    "freeuser", "friday", "friend", "friends", "froggy", "fuck", "fucked", "fucker",
    "fucking", "fuckme", "fuckoff", "fuckyou", "gabriel", "galore", "gandalf", "garcia",
    "garfield", "gateway", "gators", "gemini", "general", "genesis", "george", "georgia",
    "giants", "gibson", "ginger", "girl", "girls", "goblue", "godzilla", "golden",
    "golf", "golfer", "goober", "good", "gordon", "great", "green", "gregory",
    "guinness", "guitar", "gunner", "hahaha", "hammer", "hannah", "happy", "happy1",
    "hard", "hardcore", "hardon", "harley", "hawaii", "hawkeye", "head", "heather",
    "heaven", "heka6w2", "hello", "hello1", "helpme", "hendrix", "hentai", "hitman",
    "hobbes", "hockey", "homer", "honda", "honey", "hooker", "hooters", "horney",
    "horny", "horse", "horses", "hotdog", "hotmail", "hotrod", "house", "houston",
    "howard", "hummer", "hunter", "hunting", "iceman", "iloveyou", "indian", "infinity",
    "inside", "internet", "ireland", "ironman", "iwantu", "jack", "jackass", "jackie",
    "jackson", "jaguar", "jake", "james", "japan", "jasmine", "jason", "jasper",
    "jeff", "jeffrey", "jennifer", "jenny", "jeremy", "jerry", "jessica", "jessie",
    "jester", "jesus", "jimmy", "john", "johnny", "johnson", "jonathan", "jones",
    "jordan", "jordan23", "joseph", "joshua", "juice", "junior", "jupiter", "justice",
    "justin", "katana", "katie", "kawasaki", "kelly", "kermit", "kevin", "killer",
    "kimberly", "king", "kitten", "kitty", "knight", "kodiak", "kramer", "kristen",
    "lacrosse", "ladies", "lakers", "lasvegas", "lauren", "lawrence", "leather", "legend",
    "lesbian", "leslie", "letmein", "letmein1", "liberty", "lickme", "lifehack", "light",
    "lights", "lincoln", "lisa", "little", "liverpoo", "liverpool", "lizard", "london",
    "long", "looking", "louise", "love", "lovely", "loveme", "lover", "loverboy",
    "lovers", "lucky", "lucky1", "machine", "maddog", "madison", "madmax", "maggie",
    "magic", "magnum", "marcus", "marina", "marine", "marines", "mark", "marlboro",
    "marley", "marshall", "martin", "marvin", "maryjane", "master", "matrix", "matt",
    "matthew", "mature", "maverick", "maximus", "maxwell", "melanie", "melissa", "member",
    "mercedes", "mercury", "merlin", "metallic", "mexico", "michael", "michael1", "michelle",
    "michigan", "mickey", "midnight", "mike", "miller", "mine", "mistress", "mitchell",
    "molly", "monday", "money", "money1", "monica", "monkey", "monster", "montana",
    "mookie", "moose", "morgan", "mother", "mountain", "mouse", "movie", "mozart",
    "muffin", "murphy", "music", "mustang", "naked", "nascar", "natalie", "natasha",
    "nathan", "naughty", "ncc1701", "ncc1701d", "nelson", "nemesis", "newport", "newyork",
    "nicholas", "nicole", "nigger", "nintendo", "nipple", "nipples", "nirvana", "nissan",
    "norman", "nothing", "november", "october", "oliver", "olivia", "online", "orange",
    "oscar", "ou812", "packard", "packers", "pakistan", "pamela", "pantera", "panther",
    "panthers", "panties", "paradise", "paris", "parker", "party", "pass", "passion",
    "passport", "passw0rd", "password", "password1", "patches", "patricia", "patrick", "patriots",
    "paul", "peaches", "peanut", "pearljam", "peekaboo", "penguin", "penis", "people",
    "pepper", "pepsi", "perfect", "pervert", "peter", "phantom", "phoenix", "phpbb",
    "picard", "pimp", "pimpin", "pirate", "platinum", "playboy", "player", "please",
    "pokemon", "police", "pontiac", "poohbear", "pookie", "poop", "poopoo", "popcorn",
    "popeye", "porn", "porno", "porsche", "power", "prince", "princess", "private",
    "psycho", "pumpkin", "purple", "pussies", "pussy", "pussy1", "pussycat", "pyramid",
    "qazwsx", "qqqqqq", "qwer", "qwert", "qwerty", "qwerty1", "qwertyui", "rabbit",
    "rachel", "racing", "raider", "raiders", "rainbow", "ranger", "rangers", "raptor",
    "rascal", "raven", "raymond", "rebecca", "red123", "reddog", "redhead", "redrum",
    "redskins", "redsox", "redwings", "reggie", "remember", "richard", "robert", "rock",
    "rocket", "rocks", "rocky", "roland", "rolltide", "rooster", "rosebud", "runner",
    "rush2112", "russell", "russia", "ryan", "sabrina", "sailor", "saints", "samantha",
    "sammy", "samson", "samsung", "samuel", "sandman", "sandra", "sandy", "sarah",
    "saturn", "scarface", "school", "scooby", "scooter", "scorpio", "scorpion", "scotland",
    "scott", "scotty", "secret", "security", "semperfi", "service", "sexsex", "sexy",
    "shadow", "shaggy", "shannon", "sharon", "shaved", "shelby", "shit", "shithead",
    "shooter", "shorty", "sierra", "silver", "simon", "simple", "simpson", "simpsons",
    "single", "skipper", "skippy", "slayer", "slipknot", "slut", "sluts", "smith",
    "smokey", "smooth", "snake", "snickers", "sniper", "snoopy", "snowball", "snowman",
    "soccer", "softball", "sophie", "spanky", "sparky", "speedy", "spencer", "spider",
    "spiderma", "spike", "spirit", "spitfire", "spooky", "sports", "spring", "squirt",
    "srinivas", "stanley", "star", "stargate", "stars", "startrek", "starwars", "steelers",
    "stella", "stephen", "steve", "steven", "stewart", "sticky", "stingray", "stinky",
    "strike", "stupid", "sublime", "success", "suck", "sucker", "suckit", "suckme",
    "sugar", "summer", "sunshine", "super", "superman", "surfer", "suzuki", "sweet",
    "swimming", "swordfis", "sydney", "system", "tarheels", "tattoo", "taurus", "taylor",
    "teen", "teens", "tennis", "teresa", "test", "test123", "tester", "testing",
    "testtest", "texas", "theman", "therock", "thomas", "thumper", "thunder", "thx1138",
    "tiffany", "tiger", "tiger1", "tigers", "tigger", "time", "timothy", "tinker",
    "titanic", "tits", "tomcat", "tommy", "tony", "topgun", "toyota", "travis",
    "trinity", "trooper", "trouble", "trucks", "trustno1", "tucker", "turkey", "turtle",
    "united", "vagina", "vampire", "vanessa", "vegeta", "veronica", "victor", "victoria",
    "video", "viking", "vikings", "vincent", "viper", "virginia", "vision", "voodoo",
    "voyager", "walker", "walter", "wanker", "warrior", "water", "weasel", "welcome",
    "westside", "whatever", "white", "whore", "whynot", "wildcat", "wildcats", "william",
    "williams", "willie", "willow", "wilson", "windows", "winner", "winston", "winter",
    "wizard", "wolf", "wolfgang", "wolfpack", "wolverin", "wolves", "wombat", "women",
    "woody", "xavier", "xxxx", "xxxxx", "xxxxxx", "xxxxxxxx", "yamaha", "yankee",
    "yankees", "yellow", "young", "zachary", "zombie", "zxcvbn", "zxcvbnm", "zzzzzz",
})
# fmt: on


# ---------------------------------------------------------------------------
# Password hashing (bcrypt, work factor 12)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (work factor 12).

    Args:
        password: Plain-text password.

    Returns:
        Bcrypt hash string (``$2b$12$...``).
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plain-text password against a bcrypt hash.

    Args:
        password: Plain-text password to check.
        password_hash: Bcrypt hash to verify against.

    Returns:
        ``True`` if the password matches, ``False`` otherwise.

    Raises:
        pwdlib.exceptions.UnknownHashError: If *password_hash* is not a
            valid bcrypt string (programming error, not user input).
    """
    return _hasher.verify(password, password_hash)


def check_password_strength(password: str, *, email: str | None = None) -> list[str]:
    """Check password strength per NIST SP 800-63B guidelines.

    Checks minimum length (8 characters), common password blocklist, and
    optionally whether the password is too similar to the user's email.
    Does **not** require digits, uppercase, or special characters.

    Args:
        password: Password to evaluate.
        email: Optional email address to check against (keyword-only).

    Returns:
        List of issue descriptions.  Empty list means the password
        passes all checks.
    """
    issues: list[str] = []
    if len(password) < _MIN_PASSWORD_LENGTH:
        issues.append(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters")
    if password.lower() in _COMMON_PASSWORDS:
        issues.append("Password is too common")
    if email:
        if password.lower() == email.lower():
            issues.append("Password cannot be the same as your email address")
        elif email.split("@")[0].lower() in password.lower():
            issues.append("Password should not contain your email username")
    return issues


# ---------------------------------------------------------------------------
# Session tokens (256-bit entropy, SHA-256 storage hash)
# ---------------------------------------------------------------------------


def generate_session_token() -> str:
    """Generate a cryptographically secure session token.

    Returns:
        URL-safe base64 string (43 characters, 256-bit entropy).
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hash a token for database storage.

    Args:
        token: Raw token string.

    Returns:
        Hex-encoded SHA-256 digest (64 characters).
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# API keys (prefixed ``dango_ak_`` + SHA-256)
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str]:
    """Generate a new API key with the ``dango_ak_`` prefix.

    Returns:
        Tuple of ``(raw_key, sha256_hash)``.  The raw key is shown to
        the user once; only the hash is stored in the database.
    """
    random_part = secrets.token_urlsafe(32)
    raw_key = f"{_API_KEY_PREFIX}{random_part}"
    return raw_key, hash_api_key(raw_key)


def hash_api_key(key: str) -> str:
    """SHA-256 hash an API key for database storage.

    Args:
        key: Full API key (including ``dango_ak_`` prefix).

    Returns:
        Hex-encoded SHA-256 digest (64 characters).
    """
    return hashlib.sha256(key.encode()).hexdigest()


def get_key_prefix(key: str) -> str:
    """Return a safe-to-display prefix of an API key.

    Shows ``dango_ak_`` plus the first 3 characters of the random
    portion — enough to identify a key without exposing the secret.

    Args:
        key: Full API key.

    Returns:
        First 12 characters of the key.
    """
    return key[:12]


# ---------------------------------------------------------------------------
# Invite tokens (SHA-256, same pattern as API keys)
# ---------------------------------------------------------------------------


def generate_invite_token() -> tuple[str, str]:
    """Generate an invite token and its SHA-256 hash.

    Returns:
        Tuple of ``(raw_token, token_hash)``.  The raw token is embedded
        in the invite URL; only the hash is stored in the database.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


# ---------------------------------------------------------------------------
# Temporary passwords
# ---------------------------------------------------------------------------


def generate_temp_password(length: int = 12) -> str:
    """Generate a temporary password using unambiguous characters.

    Excludes visually ambiguous characters (``0``, ``O``, ``o``,
    ``1``, ``l``, ``I``) for readability when printed to a terminal.

    Args:
        length: Number of characters (default 12).

    Returns:
        Random alphanumeric string.
    """
    return "".join(secrets.choice(_TEMP_PASSWORD_CHARS) for _ in range(length))


# ---------------------------------------------------------------------------
# Recovery codes (TOTP 2FA backup)
# ---------------------------------------------------------------------------


def generate_recovery_codes(count: int = 8) -> list[str]:
    """Generate formatted recovery codes for TOTP 2FA backup.

    Each code is formatted as ``XXXX-XXXX`` using uppercase
    alphanumeric characters (no ambiguous characters).

    Args:
        count: Number of codes to generate (default 8).

    Returns:
        List of recovery code strings.
    """
    codes: list[str] = []
    for _ in range(count):
        left = "".join(secrets.choice(_RECOVERY_CODE_CHARS) for _ in range(4))
        right = "".join(secrets.choice(_RECOVERY_CODE_CHARS) for _ in range(4))
        codes.append(f"{left}-{right}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code for database storage.

    Normalises the code by stripping dashes and converting to
    uppercase before hashing, so ``ABCD-EFGH``, ``abcdefgh``,
    and ``ABCDEFGH`` all produce the same hash.

    Args:
        code: Recovery code (with or without dashes, any case).

    Returns:
        Hex-encoded SHA-256 digest (64 characters).
    """
    normalised = code.replace("-", "").upper()
    return hashlib.sha256(normalised.encode()).hexdigest()
