
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import streamlit as st

# ==========================================
# MEME RADAR AI - DISCOVERY + SECURITY
# ==========================================

st.set_page_config(
    page_title="Meme Radar AI",
    page_icon="🚀",
    layout="wide",
)

DEX_API = "https://api.dexscreener.com"
RUGCHECK_API = "https://api.rugcheck.xyz"
TIMEOUT = 8
HEADERS = {"User-Agent": "MemeRadarAI/3.0"}


# ==========================================
# HELPERS
# ==========================================

def get_json(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def number(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def money(value):
    value = number(value)

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"

    return f"${value:.2f}"


def age_label(hours):
    if hours is None:
        return "Unknown"
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 24:
        return f"{hours:.1f} hr"
    return f"{hours / 24:.1f} days"


# ==========================================
# DISCOVER CANDIDATES
# ==========================================

def discover_candidates():
    endpoints = [
        (
            f"{DEX_API}/token-profiles/latest/v1",
            "New profile",
        ),
        (
            f"{DEX_API}/token-boosts/latest/v1",
            "Recent boost",
        ),
        (
            f"{DEX_API}/token-boosts/top/v1",
            "Top boost",
        ),
    ]

    candidates = {}

    for url, source in endpoints:
        data = get_json(url)

        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue

            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")
            if not address:
                continue

            if address not in candidates:
                candidates[address] = {
                    "address": address,
                    "sources": set(),
                    "boosted": False,
                }

            candidates[address]["sources"].add(source)

            if "boost" in source.lower():
                candidates[address]["boosted"] = True

    for item in candidates.values():
        item["sources"] = ", ".join(sorted(item["sources"]))

    return list(candidates.values())


# ==========================================
# MARKET DATA
# ==========================================

def get_best_pair(address):
    url = f"{DEX_API}/token-pairs/v1/solana/{address}"
    data = get_json(url)

    if not isinstance(data, list):
        return None

    pairs = [
        pair for pair in data
        if isinstance(pair, dict)
        and pair.get("chainId") == "solana"
        and (pair.get("baseToken") or {}).get("address") == address
    ]

    if not pairs:
        return None

    return max(
        pairs,
        key=lambda pair: number(
            (pair.get("liquidity") or {}).get("usd")
        ),
    )


# ==========================================
# RUGCHECK SECURITY REPORT
# ==========================================

def get_rugcheck_report(address):
    """
    Retrieve the third-party token report.

    None means the report was unavailable. It does NOT
    mean the token is safe.
    """
    url = f"{RUGCHECK_API}/v1/tokens/{address}/report"
    data = get_json(url)

    if not isinstance(data, dict):
        return None

    return data


def get_report_risks(report):
    """Extract reported risks without assuming every field exists."""
    if not isinstance(report, dict):
        return []

    raw_risks = report.get("risks", [])

    if not isinstance(raw_risks, list):
        return []

    found = []

    for item in raw_risks:
        if isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("description")
                or "Unspecified risk"
            )
            level = (
                item.get("level")
                or item.get("severity")
                or "Unspecified"
            )
            description = item.get("description") or name

            found.append({
                "name": str(name),
                "level": str(level),
                "description": str(description),
            })

        elif isinstance(item, str):
            found.append({
                "name": item,
                "level": "Unspecified",
                "description": item,
            })

    return found


def get_risk_rating(report):
    """
    Give a cautious summary of the provider's reported findings.
    Missing reports never receive a safe rating.
    """
    if not isinstance(report, dict):
        return "UNKNOWN", "Security report unavailable"

    risks = get_report_risks(report)

    levels = [
        risk["level"].lower()
        for risk in risks
    ]

    if any(
        "critical" in level
        for level in levels
    ):
        return "CRITICAL", "Critical risk reported"

    if any(
        "danger" in level or "high" in level
        for level in levels
    ):
        return "HIGH", "High risk reported"

    if any(
        "medium" in level or "moderate" in level
        for level in levels
    ):
        return "MODERATE", "Moderate risk reported"

    if risks:
        return "REVIEW", "Risk findings need review"

    # An empty risk list is not proof of safety.
    return "UNCONFIRMED", "No listed risks; safety unconfirmed"


def report_score(report):
    """Display a provider score as reported, without guessing its scale."""
    if not isinstance(report, dict):
        return "Unavailable"

    for key in ("score_normalised", "scoreNormalized", "score"):
        if report.get(key) is not None:
            return str(report[key])

    return "Not provided"


def extract_authority(report, key):
    """
    Extract authority values only when their location is known.
    Unknown stays unknown.
    """
    if not isinstance(report, dict):
        return "Unknown"

    possible_objects = [
        report,
        report.get("token"),
        report.get("tokenMeta"),
        report.get("token_metadata"),
    ]

    for obj in possible_objects:
        if not isinstance(obj, dict):
            continue

        if key in obj:
            value = obj[key]

            if value is None or value == "":
                return "Disabled / None reported"

            return "Active"

    return "Unknown"


def holder_summary(report):
    """
    Use top-holder data only when present.
    The report schema can vary; otherwise return Unknown.
    """
    if not isinstance(report, dict):
        return "Unknown"

    holders = report.get("topHolders")

    if not isinstance(holders, list) or not holders:
        return "Unknown"

    percentages = []

    for holder in holders[:10]:
        if not isinstance(holder, dict):
            continue

        # Only use explicit percentage-like fields.
        for key in ("pct", "percentage", "percent"):
            if holder.get(key) is not None:
                percentages.append(number(holder[key]))
                break

    if not percentages:
        return "Holder data present; percentage unavailable"

    return f"Top listed wallets: {sum(percentages):.1f}%"


# ==========================================
# TOKEN SCORING
# ==========================================

def analyze_token(candidate, pair, report):
    token = pair.get("baseToken") or {}
    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )

    volume = pair.get("volume") or {}
    volume_5m = number(volume.get("m5"))
    volume_1h = number(volume.get("h1"))
    volume_6h = number(volume.get("h6"))
    volume_24h = number(volume.get("h24"))

    txns = pair.get("txns") or {}
    tx_5m = txns.get("m5") or {}
    tx_1h = txns.get("h1") or {}
    tx_6h = txns.get("h6") or {}
    tx_24h = txns.get("h24") or {}

    buys_1h = number(tx_1h.get("buys"))
    sells_1h = number(tx_1h.get("sells"))
    trades_1h = buys_1h + sells_1h

    trades_6h = (
        number(tx_6h.get("buys"))
        + number(tx_6h.get("sells"))
    )
    trades_24h = (
        number(tx_24h.get("buys"))
        + number(tx_24h.get("sells"))
    )

    created_ms = number(pair.get("pairCreatedAt"))
    age_hours = None

    if created_ms > 0:
        age_hours = max(
            0,
            (time.time() * 1000 - created_ms) / 3_600_000,
        )

    # EARLY-LAUNCH SCORE
    early = 0

    if age_hours is not None:
        if age_hours <= 1:
            early += 45
        elif age_hours <= 3:
            early += 40
        elif age_hours <= 6:
            early += 34
        elif age_hours <= 12:
            early += 27
        elif age_hours <= 24:
            early += 20
        elif age_hours <= 72:
            early += 10
        elif age_hours <= 168:
            early += 5

    if liquidity >= 50_000:
        early += 40
    elif liquidity >= 20_000:
        early += 35
    elif liquidity >= 10_000:
        early += 30
    elif liquidity >= 5_000:
        early += 22
    elif liquidity >= 2_000:
        early += 12
    elif liquidity >= 500:
        early += 5

    if trades_1h >= 20:
        early += 15
    elif trades_1h >= 5:
        early += 10
    elif trades_1h > 0:
        early += 5

    early = min(100, early)

    # MOMENTUM SCORE
    momentum = 0

    if volume_1h >= 100_000:
        momentum += 35
    elif volume_1h >= 25_000:
        momentum += 30
    elif volume_1h >= 10_000:
        momentum += 25
    elif volume_1h >= 2_500:
        momentum += 18
    elif volume_1h >= 500:
        momentum += 10
    elif volume_1h > 0:
        momentum += 4

    if trades_1h >= 100:
        momentum += 25
    elif trades_1h >= 40:
        momentum += 20
    elif trades_1h >= 15:
        momentum += 15
    elif trades_1h >= 5:
        momentum += 9
    elif trades_1h > 0:
        momentum += 4

    if trades_1h > 0:
        buy_ratio = buys_1h / trades_1h

        if buy_ratio >= 0.65:
            momentum += 20
        elif buy_ratio >= 0.55:
            momentum += 15
        elif buy_ratio >= 0.45:
            momentum += 10
        else:
            momentum += 3

    if volume_6h > 0 and volume_1h > (volume_6h / 6) * 1.5:
        momentum += 10
    elif volume_24h > 0 and volume_1h > (volume_24h / 24):
        momentum += 5

    if volume_5m > 0 and volume_1h > 0:
        if volume_5m > volume_1h / 12:
            momentum += 10

    momentum = min(100, momentum)

    combined = round(
        early * 0.45 + momentum * 0.55
    )

    # BASIC MARKET FLAGS
    market_flags = []

    if liquidity < 5_000:
        market_flags.append("Low liquidity")

    if volume_1h == 0:
        market_flags.append("No reported 1h volume")

    if trades_1h == 0:
        market_flags.append("No reported 1h trades")
    elif sells_1h > buys_1h:
        market_flags.append("More sells than buys (1h)")

    if age_hours is None:
        market_flags.append("Pool age unknown")

    if candidate["boosted"]:
        market_flags.append("Paid boost feed")

    fdv = number(pair.get("fdv"))

    if fdv > 0 and liquidity > 0:
        if fdv / liquidity > 1000:
            market_flags.append("FDV very high vs liquidity")

    # SECURITY REPORT
    rating, rating_description = get_risk_rating(report)
    risks = get_report_risks(report)

    risk_names = [
        f"{risk['level']}: {risk['name']}"
        for risk in risks
    ]

    security_flags = "; ".join(risk_names)

    if not security_flags:
        if report is None:
            security_flags = "Unknown: report unavailable"
        else:
            security_flags = (
                "No listed risks; not proof of safety"
            )

    return {
        "Name": token.get("name") or "Unknown",
        "Symbol": token.get("symbol") or "?",
        "Age (hours)": age_hours,
        "Age": age_label(age_hours),
        "Early score": early,
        "Momentum score": momentum,
        "Combined score": combined,
        "Liquidity": liquidity,
        "Volume 5m": volume_5m,
        "Volume 1h": volume_1h,
        "Volume 6h": volume_6h,
        "Volume 24h": volume_24h,
        "Buys 1h": int(buys_1h),
        "Sells 1h": int(sells_1h),
        "Trades 6h": int(trades_6h),
        "Trades 24h": int(trades_24h),
        "Market cap": number(pair.get("marketCap")),
        "FDV": fdv,
        "Sources": candidate["sources"],
        "Market flags": "; ".join(market_flags) or "No basic flags",
        "Security rating": rating,
        "Security summary": rating_description,
        "RugCheck score": report_score(report),
        "Security flags": security_flags,
        "Mint authority": extract_authority(report, "mintAuthority"),
        "Freeze authority": extract_authority(report, "freezeAuthority"),
        "Holder summary": holder_summary(report),
        "Security checked": report is not None,
        "Address": candidate["address"],
        "Pair URL": pair.get("url") or (
            "https://dexscreener.com/solana/"
            + candidate["address"]
        ),
        "RugCheck URL": (
            "https://rugcheck.xyz/tokens/"
            + candidate["address"]
        ),
        "Boosted": candidate["boosted"],
    }


# ==========================================
# SCAN
# ==========================================

def scan_one(candidate):
    address = candidate["address"]

    # Each token gets a market lookup and a security lookup.
    pair = get_best_pair(address)

    if pair is None:
        return None

    report = get_rugcheck_report(address)

    return analyze_token(candidate, pair, report)


def scan_tokens(limit):
    candidates = discover_candidates()[:limit]

    results = []
    errors = 0

    # Keep concurrency limited to avoid excessive API requests.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(scan_one, candidate)
            for candidate in candidates
        ]

        for future in as_completed(futures):
            try:
                row = future.result()

                if row is None:
                    errors += 1
                else:
                    results.append(row)

            except Exception:
                errors += 1

    return results, errors, len(candidates)


# ==========================================
# DASHBOARD
# ==========================================

st.title("🚀 Meme Radar AI")
st.caption(
    "Solana token discovery, momentum ranking, and "
    "independent security reports"
)

with st.sidebar:
    st.header("Scanner settings")

    scan_limit = st.slider(
        "Candidates per scan",
        min_value=5,
        max_value=30,
        value=10,
        step=5,
    )

    max_age = st.slider(
        "Maximum pool age (hours)",
        min_value=1,
        max_value=720,
        value=168,
    )

    min_liquidity = st.number_input(
        "Minimum liquidity ($)",
        min_value=0,
        max_value=1_000_000,
        value=500,
        step=500,
    )

    min_score = st.slider(
        "Minimum combined score",
        min_value=0,
        max_value=100,
        value=0,
    )

    sort_choice = st.selectbox(
        "Rank watchlist by",
        [
            "Combined score",
            "Early score",
            "Momentum score",
            "Volume 1h",
        ],
    )

if "scan_rows" not in st.session_state:
    st.session_state["scan_rows"] = []
    st.session_state["scan_time"] = None
    st.session_state["scan_errors"] = 0
    st.session_state["scan_count"] = 0

scan_col, info_col = st.columns([1, 3])

with scan_col:
    scan_clicked = st.button(
        "🔎 Scan tokens",
        type="primary",
        use_container_width=True,
    )

with info_col:
    st.write(
        "Scan recent Solana discovery feeds, rank market activity, "
        "and retrieve third-party risk reports."
    )

if scan_clicked:
    with st.spinner(
        "Scanning markets and checking token security reports..."
    ):
        rows, errors, checked = scan_tokens(scan_limit)

        st.session_state["scan_rows"] = rows
        st.session_state["scan_errors"] = errors
        st.session_state["scan_count"] = checked
        st.session_state["scan_time"] = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

rows = st.session_state["scan_rows"]

if st.session_state["scan_time"]:
    st.caption(
        f"Last scan: {st.session_state['scan_time']} | "
        f"Candidates checked: {st.session_state['scan_count']} | "
        f"Unavailable/error: {st.session_state['scan_errors']}"
    )

if not rows:
    st.info(
        "Your scanner is ready. Tap 'Scan tokens' to start."
    )
    st.stop()

# FILTERS
filtered = []

for row in rows:
    age = row["Age (hours)"]

    if age is None or age > max_age:
        continue

    if row["Liquidity"] < min_liquidity:
        continue

    if row["Combined score"] < min_score:
        continue

    filtered.append(row)

sort_key = {
    "Combined score": "Combined score",
    "Early score": "Early score",
    "Momentum score": "Momentum score",
    "Volume 1h": "Volume 1h",
}[sort_choice]

filtered.sort(
    key=lambda row: row[sort_key],
    reverse=True,
)

# SUMMARY
c1, c2, c3, c4 = st.columns(4)

c1.metric("Matching tokens", len(filtered))
c2.metric(
    "High momentum (70+)",
    sum(row["Momentum score"] >= 70 for row in filtered),
)
c3.metric(
    "Security reports available",
    sum(row["Security checked"] for row in filtered),
)
c4.metric(
    "Critical/high reports",
    sum(
        row["Security rating"] in ("CRITICAL", "HIGH")
        for row in filtered
    ),
)

st.subheader("Ranked watchlist")

if not filtered:
    st.warning(
        "No tokens match the filters. Lower the minimum liquidity "
        "or score, or increase the maximum pool age."
    )
    st.stop()

st.caption(
    "Market scores and security reports are separate. "
    "Unknown means unverified, not safe."
)

for index, row in enumerate(filtered, start=1):
    with st.container(border=True):
        left, middle, right = st.columns([3, 2, 1])

        with left:
            st.markdown(
                f"### #{index} {row['Name']} (${row['Symbol']})"
            )
            st.caption(
                f"Combined: {row['Combined score']}/100 | "
                f"Early: {row['Early score']}/100 | "
                f"Momentum: {row['Momentum score']}/100"
            )
            st.caption(
                f"Pool age: {row['Age']} | "
                f"Buys/sells (1h): "
                f"{row['Buys 1h']}/{row['Sells 1h']}"
            )

        with middle:
            st.write(f"Liquidity: {money(row['Liquidity'])}")
            st.write(f"Volume (1h): {money(row['Volume 1h'])}")
            st.write(f"Volume (24h): {money(row['Volume 24h'])}")
            st.write(f"Market cap: {money(row['Market cap'])}")

        with right:
            st.link_button(
                "View token",
                row["Pair URL"],
                use_container_width=True,
            )
            st.link_button(
                "RugCheck report",
                row["RugCheck URL"],
                use_container_width=True,
            )

        rating = row["Security rating"]

        if rating == "CRITICAL":
            st.error(f"SECURITY: {rating} - {row['Security summary']}")
        elif rating == "HIGH":
            st.error(f"SECURITY: {rating} - {row['Security summary']}")
        elif rating == "MODERATE":
            st.warning(f"SECURITY: {rating} - {row['Security summary']}")
        elif rating == "REVIEW":
            st.warning(f"SECURITY: REVIEW - {row['Security summary']}")
        elif rating == "UNKNOWN":
            st.warning("SECURITY: UNKNOWN - report unavailable")
        else:
            st.info(
                f"SECURITY: {rating} - {row['Security summary']}"
            )

        with st.expander("Inspect security details"):
            a, b, c = st.columns(3)

            a.write(f"Provider score: {row['RugCheck score']}")
            b.write(f"Mint authority: {row['Mint authority']}")
            c.write(f"Freeze authority: {row['Freeze authority']}")

            st.write(f"Holder concentration: {row['Holder summary']}")
            st.write(f"Reported risks: {row['Security flags']}")
            st.write(f"Market warnings: {row['Market flags']}")
            st.write(f"Discovery source: {row['Sources']}")
            st.code(row["Address"], language=None)

st.divider()
st.caption(
    "Research tool only. Reports may be missing, delayed, or incomplete. "
    "A clean report is not a guarantee against a rug pull. "
    "Verify major findings independently before risking money."
)
