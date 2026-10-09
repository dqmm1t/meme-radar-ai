
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import streamlit as st

# ==========================================
# MEME RADAR AI - SOLANA DISCOVERY SCANNER
# ==========================================

st.set_page_config(
    page_title="Meme Radar AI",
    page_icon="🚀",
    layout="wide",
)

API = "https://api.dexscreener.com"
TIMEOUT = 8
HEADERS = {"User-Agent": "MemeRadarAI/2.0"}


# ==========================================
# HELPERS
# ==========================================

def get_json(url):
    """Fetch JSON with a timeout so requests do not hang forever."""
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
# DISCOVER TOKEN CANDIDATES
# ==========================================

def discover_candidates():
    """
    Discover candidates from recent profiles and boost feeds.
    These feeds are not a complete list of every Solana launch.
    """
    endpoints = [
        (
            f"{API}/token-profiles/latest/v1",
            "New profile",
        ),
        (
            f"{API}/token-boosts/latest/v1",
            "Recent boost",
        ),
        (
            f"{API}/token-boosts/top/v1",
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
# LOAD MARKET DATA
# ==========================================

def get_token_pair(candidate):
    address = candidate["address"]
    url = f"{API}/token-pairs/v1/solana/{address}"
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

    # Prefer the pool with the most reported liquidity.
    pair = max(
        pairs,
        key=lambda p: number(
            (p.get("liquidity") or {}).get("usd")
        ),
    )

    return candidate, pair


# ==========================================
# SCORING
# ==========================================

def analyze_token(candidate, pair):
    token = pair.get("baseToken") or {}
    liquidity = number((pair.get("liquidity") or {}).get("usd"))

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
    buys_5m = number(tx_5m.get("buys"))
    sells_5m = number(tx_5m.get("sells"))

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

    # EARLY SCORE: max 100
    # Rewards recent pools and usable liquidity.
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

    # MOMENTUM SCORE: max 100
    # Rewards current activity and transaction balance.
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

    # Positive buy/sell balance is one signal, not proof of demand.
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

    # Compare hourly activity to longer-window averages.
    if volume_6h > 0 and volume_1h > (volume_6h / 6) * 1.5:
        momentum += 10
    elif volume_24h > 0 and volume_1h > (volume_24h / 24):
        momentum += 5

    if volume_5m > 0 and (
        volume_1h > 0
        and volume_5m > volume_1h / 12
    ):
        momentum += 10

    momentum = min(100, momentum)

    # COMBINED SCORE: early discovery + momentum.
    # Neither score is a prediction of future returns.
    combined = round(early * 0.45 + momentum * 0.55)

    # Basic warning flags for manual investigation.
    warnings = []

    if liquidity < 5_000:
        warnings.append("Low liquidity")

    if volume_1h == 0:
        warnings.append("No reported 1h volume")

    if trades_1h == 0:
        warnings.append("No reported 1h trades")
    elif sells_1h > buys_1h:
        warnings.append("More sells than buys (1h)")

    if age_hours is None:
        warnings.append("Pool age unknown")

    if candidate["boosted"]:
        warnings.append("Paid boost feed")

    if number(pair.get("fdv")) > 0 and liquidity > 0:
        fdv = number(pair.get("fdv"))
        if fdv / liquidity > 1000:
            warnings.append("FDV very high vs liquidity")

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
        "FDV": number(pair.get("fdv")),
        "Sources": candidate["sources"],
        "Warnings": ", ".join(warnings) or "No basic flags",
        "Address": candidate["address"],
        "Pair URL": pair.get("url") or (
            "https://dexscreener.com/solana/"
            + candidate["address"]
        ),
        "Boosted": candidate["boosted"],
    }


# ==========================================
# RUN A SCAN
# ==========================================

def scan_tokens(limit):
    candidates = discover_candidates()

    # Scan newest discovered candidates first.
    candidates = candidates[:limit]

    results = []
    errors = 0

    # Concurrent requests reduce waiting time.
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(get_token_pair, candidate)
            for candidate in candidates
        ]

        for future in as_completed(futures):
            try:
                result = future.result()

                if result is None:
                    errors += 1
                    continue

                candidate, pair = result
                row = analyze_token(candidate, pair)
                results.append(row)

            except Exception:
                errors += 1

    return results, errors, len(candidates)


# ==========================================
# DASHBOARD
# ==========================================

st.title("🚀 Meme Radar AI")
st.caption(
    "Solana early launches + momentum tracking | "
    "Market data powered by DexScreener"
)

with st.sidebar:
    st.header("Scanner settings")

    scan_limit = st.slider(
        "Candidates per scan",
        min_value=5,
        max_value=30,
        value=15,
        step=5,
    )

    max_age = st.slider(
        "Maximum pool age (hours)",
        min_value=1,
        max_value=720,
        value=168,
        step=1,
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
        "Scan recent discovery feeds, inspect Solana pools, "
        "and rank candidates by launch age and market activity."
    )

if scan_clicked:
    with st.spinner(
        "Checking discovery feeds and token markets..."
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
        f"Unavailable: {st.session_state['scan_errors']}"
    )

if not rows:
    st.info(
        "Your scanner is ready. Select 'Scan tokens' to "
        "load recent discoveries and market activity."
    )
    st.stop()

# Filters
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

# Summary metrics
c1, c2, c3, c4 = st.columns(4)

c1.metric("Matching tokens", len(filtered))
c2.metric(
    "Top combined score",
    max(
        [row["Combined score"] for row in filtered],
        default=0,
    ),
)
c3.metric(
    "High momentum (70+)",
    sum(row["Momentum score"] >= 70 for row in filtered),
)
c4.metric(
    "Very new pools (under 6h)",
    sum(
        row["Age (hours)"] < 6
        for row in filtered
    ),
)

st.subheader("Ranked watchlist")

if not filtered:
    st.warning(
        "No tokens match your current filters. "
        "Lower minimum liquidity or score, or increase "
        "maximum pool age, then scan again."
    )
    st.stop()

st.caption(
    "Scores are heuristic rankings based on available market data. "
    "They do not establish safety, predict returns, or guarantee "
    "that a token is genuinely new."
)

for index, row in enumerate(filtered, start=1):
    with st.container(border=True):
        left, middle, right = st.columns([3, 2, 1])

        with left:
            st.markdown(
                f"### #{index} {row['Name']} "
                f"(${row['Symbol']})"
            )
            st.caption(
                f"Combined: {row['Combined score']}/100 | "
                f"Early: {row['Early score']}/100 | "
                f"Momentum: {row['Momentum score']}/100"
            )
            st.caption(
                f"Pool age: {row['Age']} | "
                f"Buys/sells in 1h: "
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

        st.caption(f"Discovery source: {row['Sources']}")
        st.caption(f"Risk flags: {row['Warnings']}")
        st.code(row["Address"], language=None)

st.divider()
st.caption(
    "Research tool only. Meme coins are highly speculative. "
    "Low liquidity, manipulated volume, paid promotion, and "
    "concentrated ownership can create substantial risk."
)
